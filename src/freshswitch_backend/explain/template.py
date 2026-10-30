from freshswitch_backend.explain.formatting import (
    format_delta_krw,
    format_fill,
    format_krw,
)
from freshswitch_backend.explain.models import (
    AlternativeInput,
    ExplainContext,
    Explanation,
    ShipperImpact,
    ShipperMessage,
)


def _recommended(ctx: ExplainContext) -> AlternativeInput | None:
    return next(
        (alt for alt in ctx.alternatives if alt.id == ctx.recommended_alternative_id),
        None,
    )


def _shipper_message(ctx: ExplainContext, shipper: ShipperImpact) -> str:
    parts = [f"{shipper.shipper_name}님, {ctx.event_summary} 건으로 적재 계획이 조정되었습니다."]
    if shipper.delta_krw == 0:
        parts.append(f"부담 비용은 {format_krw(shipper.cost_after_krw)}으로 변동 없습니다.")
    else:
        direction = "늘어납니다" if shipper.delta_krw > 0 else "줄어듭니다"
        parts.append(
            f"부담 비용이 {format_krw(shipper.cost_before_krw)}에서 "
            f"{format_krw(shipper.cost_after_krw)}으로 {format_delta_krw(shipper.delta_krw)} "
            f"{direction}."
        )
    if shipper.moved:
        parts.append(f"화물은 {shipper.moved}(으)로 옮겨집니다.")
    return " ".join(parts)


def _operator_summary(ctx: ExplainContext) -> str:
    parts = [f"{ctx.event_summary}."]
    recommended = _recommended(ctx)
    if recommended:
        fills = ", ".join(
            f"{fill.container_id} {format_fill(fill.before, fill.after, fill.capacity)}"
            for fill in recommended.container_fill
        )
        line = (
            f"추천안은 '{recommended.label}'이며 총 비용 변화는 "
            f"{format_delta_krw(recommended.total_cost_delta_krw)}입니다."
        )
        parts.append(line + (f" 적재: {fills}." if fills else ""))
    infeasible = [alt for alt in ctx.alternatives if not alt.feasible]
    for alt in infeasible:
        reason = alt.reason or "사유 미기재"
        parts.append(f"'{alt.label}' 불가: {reason}.")
    affected = [s.shipper_name for s in ctx.shippers if s.delta_krw != 0 or s.moved]
    if affected:
        parts.append(f"영향 화주: {', '.join(affected)}.")
    return " ".join(parts)


def render_template(ctx: ExplainContext) -> Explanation:
    return Explanation(
        operator_summary=_operator_summary(ctx),
        shipper_messages=[
            ShipperMessage(shipper_id=s.shipper_id, message=_shipper_message(ctx, s))
            for s in ctx.shippers
        ],
        generated_by="template",
        model=None,
    )
