from typing import Optional

import pandera.pandas as pa
from pandera.typing import Index, Series


class RawDataSchema(pa.DataFrameModel):
    """Contract for freshly ingested training data, before cleaning."""

    Date: Index[pa.DateTime]
    PRICE_EUR_MWH: Series[float] = pa.Field(alias="PRICE (EUR/MWH)", nullable=True)
    DEMAND_MW: Series[float] = pa.Field(alias="DEMAND (MW)", nullable=True)
    NUCLEAR_AVAIL_MW: Series[float] = pa.Field(alias="NUCLEAR AVAIL. (MW)", nullable=True)
    PARIS_AVGTEMP_C: Series[float] = pa.Field(nullable=True)
    LYON_AVGTEMP_C: Series[float] = pa.Field(nullable=True)
    BORDEAUX_AVGTEMP_C: Series[float] = pa.Field(nullable=True)
    MARSEILLE_AVGTEMP_C: Series[float] = pa.Field(nullable=True)

    class Config(pa.DataFrameModel.Config):
        strict = False
        coerce = True


class ProcessedDataSchema(pa.DataFrameModel):
    """
    Contract for cleaned training data: gap-free daily index, no missing values.

    Everything downstream of preprocessing (feature engineering, backtesting,
    training) is typed against this schema.
    """

    Date: Index[pa.DateTime]
    PRICE_EUR_MWH: Series[float] = pa.Field(alias="PRICE (EUR/MWH)", nullable=False)
    DEMAND_MW: Series[float] = pa.Field(alias="DEMAND (MW)", nullable=False)
    NUCLEAR_AVAIL_MW: Series[float] = pa.Field(alias="NUCLEAR AVAIL. (MW)", nullable=False)
    PARIS_AVGTEMP_C: Series[float] = pa.Field(nullable=False)
    LYON_AVGTEMP_C: Series[float] = pa.Field(nullable=False)
    BORDEAUX_AVGTEMP_C: Series[float] = pa.Field(nullable=False)
    MARSEILLE_AVGTEMP_C: Series[float] = pa.Field(nullable=False)

    class Config(pa.DataFrameModel.Config):
        strict = False
        coerce = True


class FutureExogenousSchema(pa.DataFrameModel):
    """
    Contract for forward-looking inference input.

    Only nuclear availability is genuinely known ahead of time (it is published
    as a maintenance schedule). Price, demand and the regional temperatures are
    expected to be absent and are manufactured by the exogenous simulation
    cascade, so they are nullable here and the frame is not required to carry
    them at all.
    """

    Date: Index[pa.DateTime]
    NUCLEAR_AVAIL_MW: Series[float] = pa.Field(
        alias="NUCLEAR AVAIL. (MW)", nullable=False
    )
    PRICE_EUR_MWH: Optional[Series[float]] = pa.Field(
        alias="PRICE (EUR/MWH)", nullable=True
    )
    DEMAND_MW: Optional[Series[float]] = pa.Field(
        alias="DEMAND (MW)", nullable=True
    )
    PARIS_AVGTEMP_C: Optional[Series[float]] = pa.Field(nullable=True)
    LYON_AVGTEMP_C: Optional[Series[float]] = pa.Field(nullable=True)
    BORDEAUX_AVGTEMP_C: Optional[Series[float]] = pa.Field(nullable=True)
    MARSEILLE_AVGTEMP_C: Optional[Series[float]] = pa.Field(nullable=True)

    class Config(pa.DataFrameModel.Config):
        strict = False
        coerce = True
