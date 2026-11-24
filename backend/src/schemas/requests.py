from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


class Import(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    csv: str = Field(min_length=1, max_length=1_000_000)


class Run(BaseModel):
    dataset_id: int = Field(gt=0)
    options: dict = Field(default_factory=dict)


class Review(BaseModel):
    decision: Literal['approved', 'returned']
    note: str = Field(min_length=2, max_length=1000)


class Delivery(BaseModel):
    recipient: str = Field(min_length=2, max_length=100)
    cadence: Literal['once', 'daily']
    scheduled_at: datetime | None = None


class DispatchBatch(BaseModel):
    batch_id: str = Field(min_length=1, max_length=60)
    loading_bay: str = Field(min_length=1, max_length=60)
    planned_at: str = Field(min_length=1, max_length=32)
    elapsed_min: int = Field(ge=1, le=1440)


class DispatchCreate(BaseModel):
    dispatch_no: str = Field(min_length=2, max_length=60)
    run_id: int = Field(gt=0)
    payload: list[DispatchBatch] = Field(min_length=1, max_length=2000)


class DispatchCorrect(BaseModel):
    payload: list[DispatchBatch] = Field(min_length=1, max_length=2000)
