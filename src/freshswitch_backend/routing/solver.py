import math
import time
from datetime import datetime, timedelta

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from freshswitch_backend.routing.models import (
    PickupPlan,
    PickupRequest,
    PickupStop,
    StopVisit,
    TruckRoute,
)

EARTH_RADIUS_KM = 6371.0
DROP_PENALTY = 10**9


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _assumptions(req: PickupRequest) -> list[str]:
    return [
        f"가정: 도로거리 = 직선(haversine) 거리 × {req.road_factor}",
        f"가정: 평균 주행속도 {req.speed_kmh:g} km/h",
        f"가정: 산지 상차 작업 {req.service_minutes}분/정차",
        f"가정: km당 운행비 {req.cost_per_km:,.0f}원",
        "가정: 트럭은 항만에서 출발해 산지를 돌고 CY cut-off 전에 항만으로 돌아온다",
        "가정: 한 정차지 물량은 트럭 한 대에 싣는다 (분할 집하 없음)",
    ]


def _floor_min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def solve_pickup(request: PickupRequest, time_limit_s: float = 5.0) -> PickupPlan:
    started = time.perf_counter()
    req = request
    points = [(req.depot.lat, req.depot.lon)] + [(s.lat, s.lon) for s in req.stops]
    n = len(points)
    km = [
        [
            haversine_km(*points[i], *points[j]) * req.road_factor if i != j else 0.0
            for j in range(n)
        ]
        for i in range(n)
    ]
    travel = [[math.ceil(km[i][j] / req.speed_kmh * 60) for j in range(n)] for i in range(n)]
    service = [0] + [req.service_minutes for _ in req.stops]

    t0_candidates = [
        s.ready_at - timedelta(minutes=travel[0][k + 1]) for k, s in enumerate(req.stops)
    ]
    if req.trucks_available_from is not None:
        t0_candidates.append(req.trucks_available_from)
    t0 = _floor_min(min(t0_candidates, default=req.deadline))

    def to_min(dt: datetime) -> int:
        return math.floor((dt - t0).total_seconds() / 60)

    def to_dt(m: int) -> datetime:
        return t0 + timedelta(minutes=m)

    deadline = to_min(req.deadline)
    max_cap = max((t.capacity_pallets for t in req.trucks), default=0)
    start_min = max(0, to_min(req.trucks_available_from)) if req.trucks_available_from else 0

    windows: dict[int, tuple[int, int]] = {}
    pre_dropped: list[str] = []
    for k, stop in enumerate(req.stops):
        node = k + 1
        lo = max(to_min(stop.ready_at) if stop.ready_at > t0 else 0, start_min + travel[0][node])
        hi = deadline - service[node] - travel[node][0]
        if lo > hi or stop.pallets > max_cap:
            pre_dropped.append(stop.id)
        else:
            windows[node] = (lo, hi)

    routes: list[TruckRoute] = []
    dropped = list(pre_dropped)
    status = "NO_STOPS"

    if windows and req.trucks:
        active = sorted(windows)
        nodes = [0] + active
        m = len(nodes)
        manager = pywrapcp.RoutingIndexManager(m, len(req.trucks), 0)
        routing = pywrapcp.RoutingModel(manager)

        def dist_cb(i: int, j: int) -> int:
            a, b = nodes[manager.IndexToNode(i)], nodes[manager.IndexToNode(j)]
            return round(km[a][b] * 1000)

        def time_cb(i: int, j: int) -> int:
            a, b = nodes[manager.IndexToNode(i)], nodes[manager.IndexToNode(j)]
            return service[a] + travel[a][b]

        def demand_cb(i: int) -> int:
            node = nodes[manager.IndexToNode(i)]
            return 0 if node == 0 else req.stops[node - 1].pallets

        dist_idx = routing.RegisterTransitCallback(dist_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(dist_idx)
        if req.truck_fixed_cost > 0 and req.cost_per_km > 0:
            routing.SetFixedCostOfAllVehicles(round(req.truck_fixed_cost / req.cost_per_km * 1000))

        demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
        routing.AddDimensionWithVehicleCapacity(
            demand_idx, 0, [t.capacity_pallets for t in req.trucks], True, "Load"
        )

        time_idx = routing.RegisterTransitCallback(time_cb)
        horizon = max(deadline, 0)
        routing.AddDimension(time_idx, horizon, horizon, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")
        for pos, node in enumerate(nodes[1:], start=1):
            index = manager.NodeToIndex(pos)
            time_dim.CumulVar(index).SetRange(*windows[node])
            routing.AddDisjunction([index], DROP_PENALTY)
        for v in range(len(req.trucks)):
            time_dim.CumulVar(routing.Start(v)).SetRange(start_min, horizon)
            time_dim.CumulVar(routing.End(v)).SetRange(start_min, horizon)

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        )
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
        )
        params.time_limit.FromMilliseconds(max(1, round(time_limit_s * 1000)))

        solution = routing.SolveWithParameters(params)
        if solution is None:
            status = "INFEASIBLE"
            dropped.extend(req.stops[node - 1].id for node in active)
        else:
            status = "OK"
            for pos, node in enumerate(nodes[1:], start=1):
                index = manager.NodeToIndex(pos)
                if solution.Value(routing.NextVar(index)) == index:
                    dropped.append(req.stops[node - 1].id)
            for v, truck in enumerate(req.trucks):
                seq: list[int] = []
                index = solution.Value(routing.NextVar(routing.Start(v)))
                while not routing.IsEnd(index):
                    seq.append(nodes[manager.IndexToNode(index)])
                    index = solution.Value(routing.NextVar(index))
                if seq:
                    routes.append(
                        _build_route(
                            req,
                            truck.id,
                            truck.capacity_pallets,
                            seq,
                            windows,
                            km,
                            travel,
                            service,
                            start_min,
                            deadline,
                            to_dt,
                        )
                    )
    elif windows:
        status = "NO_TRUCKS"
        dropped.extend(req.stops[node - 1].id for node in windows)

    if dropped and status == "OK":
        status = "PARTIAL"
    order = {s.id: i for i, s in enumerate(req.stops)}
    dropped.sort(key=order.__getitem__)
    total_km = round(sum(r.km for r in routes), 1)
    total_cost = round(sum(r.cost for r in routes))
    return PickupPlan(
        status=status,
        deadline=req.deadline,
        routes=routes,
        total_km=total_km,
        total_cost=total_cost,
        dropped_stop_ids=dropped,
        solve_ms=round((time.perf_counter() - started) * 1000),
        assumptions=_assumptions(req),
    )


def _build_route(
    req: PickupRequest,
    truck_id: str,
    capacity: int,
    seq: list[int],
    windows: dict[int, tuple[int, int]],
    km: list[list[float]],
    travel: list[list[int]],
    service: list[int],
    start_min: int,
    deadline: int,
    to_dt,
) -> TruckRoute:
    earliest: list[int] = []
    prev, t = 0, start_min
    for node in seq:
        t = max(windows[node][0], t + service[prev] + travel[prev][node])
        earliest.append(t)
        prev = node
    port_eta = earliest[-1] + service[seq[-1]] + travel[seq[-1]][0]

    latest = [0] * len(seq)
    nxt_time, nxt = deadline, 0
    for k in range(len(seq) - 1, -1, -1):
        node = seq[k]
        latest[k] = min(windows[node][1], nxt_time - travel[node][nxt] - service[node])
        nxt_time, nxt = latest[k], node

    first = seq[0]
    earliest_departure = earliest[0] - travel[0][first]
    latest_departure = latest[0] - travel[0][first]

    visits: list[StopVisit] = []
    load, prev, route_km = 0, 0, 0.0
    for k, node in enumerate(seq):
        stop: PickupStop = req.stops[node - 1]
        load += stop.pallets
        route_km += km[prev][node]
        visits.append(
            StopVisit(
                stop_id=stop.id,
                packhouse_id=stop.packhouse_id,
                lot_ids=stop.lot_ids,
                sequence=k + 1,
                pallets=stop.pallets,
                load_after=load,
                km_from_prev=round(km[prev][node], 1),
                ready_at=stop.ready_at,
                eta=to_dt(earliest[k]),
                latest_arrival=to_dt(latest[k]),
            )
        )
        prev = node
    route_km += km[prev][0]
    return TruckRoute(
        truck_id=truck_id,
        capacity_pallets=capacity,
        load_pallets=load,
        stops=visits,
        km=round(route_km, 1),
        cost=round(route_km * req.cost_per_km + req.truck_fixed_cost),
        earliest_departure=to_dt(earliest_departure),
        latest_departure=to_dt(latest_departure),
        port_eta=to_dt(port_eta),
        slack_minutes=deadline - port_eta,
    )
