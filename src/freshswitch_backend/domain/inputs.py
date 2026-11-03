from typing import Annotated

from pydantic import Field

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
