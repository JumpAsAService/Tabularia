"""Synthetic pydantic models for the differential test of internal/pyd: every type,
constraint and validator the gateway's models use, in combinations the real models
do not have. gen_models.py writes their Go twins (synthetic_gen_test.go)."""
import enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, Strict, StrictBool, StrictFloat, StrictInt,
                      StrictStr, field_validator, model_validator)


class Scalars(BaseModel):
    s: str
    i: int
    f: float
    b: bool


class OptScalars(BaseModel):
    s: Optional[str] = None
    i: Optional[int] = None
    f: Optional[float] = None
    b: Optional[bool] = None
    a: Any = None


class StrictScalars(BaseModel):
    si: Annotated[int, Strict()] = Field(default=0, ge=0, le=1000)
    sb: StrictBool = False
    sf: StrictFloat = 0.0
    ss: StrictStr = ""


GSHEETS_RANGE = r"^(|[A-Za-z]{1,3}[0-9]{0,7}:[A-Za-z]{1,3}[0-9]{0,7})$"


class Constrained(BaseModel):
    short: str = Field(default="", min_length=2, max_length=3)
    one: str = Field(default="x", min_length=1, max_length=1)
    pat: str = Field(default="abc", pattern=r"^[a-z]+$")
    rng: str = Field(default="", max_length=30, pattern=GSHEETS_RANGE)
    both: str = Field(default="abcd", min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    n: int = Field(default=5, ge=1, le=10)
    big: int = Field(default=0, ge=0, le=1_000_000_000)
    x: float = Field(default=0, ge=0, le=3600)
    g: int = Field(default=11, gt=10, lt=20)
    contra: int = Field(default=0, ge=10, le=1)
    fl: float = Field(default=1.0, gt=0.5, lt=1.5)
    fb: float = Field(default=2.0, ge=1.0)
    fneg: float = Field(default=0, ge=-2.5e-7, le=1e16)
    lst: list[str] = Field(default_factory=list, max_length=2)
    lst1: list[int] = Field(default_factory=list, max_length=1)
    lst_min: list[int] = Field(default_factory=list, min_length=1)
    lst_both: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    dct: dict[str, int] = Field(default_factory=dict, max_length=1)
    dct2: dict[str, str] = Field(default_factory=dict, max_length=2)
    opt_s: Optional[str] = Field(default=None, max_length=3)
    opt_i: Optional[int] = Field(default=None, ge=1)
    opt_l: Optional[list[str]] = Field(default=None, max_length=2)
    opt_p: Optional[str] = Field(default=None, pattern=r"^[a-z_][a-z0-9_]{0,62}$")


class Inner(BaseModel):
    a: int
    b: str = "x"


class Capability(str, enum.Enum):
    VIEW = "view"
    RUN = "run"
    EDIT = "edit"
    CONNECT = "connect"
    MANAGE = "manage"


ReportParamValue = Union[str, int, float, bool, None, list[Union[str, int, float, bool]]]


class Nested(BaseModel):
    inner: Inner = Field(default_factory=lambda: Inner(a=1))
    inners: list[Inner] = []
    opt: Optional[Inner] = None
    m: dict[str, Inner] = {}
    grid: list[Annotated[list[Optional[Inner]], Field(max_length=2)]] = Field(default_factory=list, max_length=3)
    anyl: list[Any] = Field(default_factory=list, max_length=4)
    anyd: dict[str, Any] = {}
    bared: dict = {}
    barel: list[dict] = Field(default_factory=list)
    lit: Literal["info", "warning", "danger"] = "warning"
    lit1: Literal["only"] = "only"
    optlit: Optional[Literal["a", "b"]] = None
    cap: Capability = Capability.VIEW
    u: Union[int, str] = 0
    rp: dict[str, ReportParamValue] = Field(default_factory=dict)
    su: dict[str, Union[StrictBool, StrictInt, StrictFloat, StrictStr, None]] = Field(default_factory=dict, max_length=3)
    dl: dict[str, Literal["file", "path", "text"]] = Field(default_factory=dict)
    ints: list[int] = []
    optl: Optional[list[int]] = None


class RequiredNested(BaseModel):
    inner: Inner
    name: str


def _check(v: str) -> str:
    if v == "bad":
        raise ValueError("bad value")
    if v == "worse":
        raise ValueError("it's \"worse\": \\ ok")
    return v


def no_dot_dot(path: str) -> str:
    if ".." in (path or "").replace("\\", "/").split("/"):
        raise ValueError("'..' is not allowed: write the folder you mean")
    return path


Checked = Annotated[str, AfterValidator(_check)]


class Validated(BaseModel):
    pw: Checked = ""
    opt_pw: Optional[Checked] = None
    folder: str = ""
    pattern: str = Field(default="x", min_length=1, max_length=5)
    templates: dict[str, str] = Field(default_factory=dict, max_length=2)

    @field_validator("folder", "pattern")
    @classmethod
    def _no_dot_dot(cls, v: str) -> str:
        return no_dot_dot(v)

    @field_validator("templates")
    @classmethod
    def _short(cls, v: dict[str, str]) -> dict[str, str]:
        if any(len(t) > 4 or len(k) > 3 for k, t in v.items()):
            raise ValueError("template too long")
        return v

    @model_validator(mode="after")
    def _whole(self) -> "Validated":
        if self.folder == "boom":
            raise ValueError(f"the folder '{self.folder}' exploded")
        return self


class Child(Validated):
    """A subclass: the parent's fields first, its own after, one overridden in place."""
    extra_field: int = 0
    pw: str = "over"


class Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_: Optional[str] = Field(default=None, alias="schema", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    n: int = 0
    sub: dict[str, Inner] = {}


class Dt(BaseModel):
    when: Optional["datetime"] = None


from datetime import datetime  # noqa: E402

Dt.model_rebuild()

MODELS = [Scalars, OptScalars, StrictScalars, Constrained, Inner, Nested, RequiredNested, Validated, Child, Forbid, Dt]
