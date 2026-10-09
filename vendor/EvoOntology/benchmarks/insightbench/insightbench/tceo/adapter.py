"""Implementation for the insightbench.insightbench.tceo.adapter module."""

import re
from typing import List, Optional

import pandas as pd
from pandas.api.types import (
    is_bool_dtype,
    is_datetime64_any_dtype,
    is_numeric_dtype,
)

from insightbench.tceo.models import ColumnProfile, JoinCandidate, TaskInventory

_TIME_NAME = re.compile(
    r"(^|_)(date|time|timestamp|datetime|year|month|day)(_|$)|(_at|_on|_date|_time|_timestamp)$|^(opened|closed|created|updated|start|started|end|ended)$",
    re.IGNORECASE,
)
_ID_NAME = re.compile(
    r"(^|_)(id|key|code|number|no|uuid|email|user_name|username)(_|$)",
    re.IGNORECASE,
)
_MAX_SAMPLE_VALUES = 5


class InsightAdapter:
    """Implementation of InsightAdapter."""



    def build(
        self, table: pd.DataFrame, table_user: Optional[pd.DataFrame] = None
    ) -> TaskInventory:
        """Build the requested value."""
        columns = self._profile_table(table, "main")
        joins: List[JoinCandidate] = []
        if table_user is not None:
            columns.extend(self._profile_table(table_user, "user"))
            joins = self._find_joins(table, table_user)
        return TaskInventory(columns=columns, joins=joins)



    def _profile_table(self, table: pd.DataFrame, source: str) -> List[ColumnProfile]:
        row_count = len(table)
        profiles = []
        for name in table.columns:
            series = table[name]
            unique_count = int(series.nunique(dropna=True))
            unique_ratio = unique_count / max(row_count, 1)
            missing_rate = float(series.isna().mean()) if row_count else 0.0


            role = self._infer_role(series, str(name), unique_ratio)


            safe_samples = self._extract_sample_values(series)


            numeric_stats = self._extract_numeric_stats(series, role)


            time_range = self._extract_time_range(series, role)

            profiles.append(
                ColumnProfile(
                    source=source,
                    name=str(name),
                    dtype=str(series.dtype),
                    role=role,
                    row_count=row_count,
                    missing_rate=missing_rate,
                    unique_count=unique_count,
                    unique_ratio=float(unique_ratio),
                    sample_values=safe_samples,
                    numeric_min=numeric_stats.get("min"),
                    numeric_max=numeric_stats.get("max"),
                    numeric_mean=numeric_stats.get("mean"),
                    numeric_p25=numeric_stats.get("p25"),
                    numeric_p50=numeric_stats.get("p50"),
                    numeric_p75=numeric_stats.get("p75"),
                    time_min=time_range.get("min"),
                    time_max=time_range.get("max"),
                )
            )
        return profiles



    @staticmethod
    def _extract_sample_values(series: pd.Series) -> List[str]:
        """Extract sample values."""
        if len(series) == 0:
            return []
        non_null = series.dropna()
        if len(non_null) == 0:
            return []
        unique_vals = non_null.unique()

        samples = [str(v) for v in unique_vals[:_MAX_SAMPLE_VALUES]]
        return samples



    @staticmethod
    def _extract_numeric_stats(series: pd.Series, role: str) -> dict:
        """Extract numeric stats."""
        if len(series) == 0 or series.isna().all():
            return {}

        if is_numeric_dtype(series) and not is_bool_dtype(series):
            pass
        elif role == "measure":

            try:
                series = pd.to_numeric(series, errors="coerce")
            except Exception:
                return {}
            if series.isna().all():
                return {}
        else:
            return {}

        non_null = series.dropna()
        if len(non_null) == 0:
            return {}

        try:
            return {
                "min": float(non_null.min()),
                "max": float(non_null.max()),
                "mean": float(non_null.mean()),
                "p25": float(non_null.quantile(0.25)),
                "p50": float(non_null.quantile(0.50)),
                "p75": float(non_null.quantile(0.75)),
            }
        except Exception:
            return {}



    @staticmethod
    def _extract_time_range(series: pd.Series, role: str) -> dict:
        """Extract time range."""
        if len(series) == 0 or series.isna().all():
            return {}

        non_null = series.dropna()
        if len(non_null) == 0:
            return {}


        if is_datetime64_any_dtype(series.dtype):
            return {
                "min": str(non_null.min()),
                "max": str(non_null.max()),
            }


        if role == "time":
            try:
                parsed = pd.to_datetime(non_null, errors="coerce")
                valid = parsed.dropna()
                if len(valid) == 0:
                    return {}
                return {
                    "min": str(valid.min()),
                    "max": str(valid.max()),
                }
            except Exception:
                return {}

        return {}



    @staticmethod
    def _infer_role(series: pd.Series, name: str, unique_ratio: float) -> str:
        """Infer role."""
        if series.notna().sum() == 0:
            return "unknown"
        if is_datetime64_any_dtype(series.dtype) or _TIME_NAME.search(name):
            return "time"
        if _ID_NAME.search(name):
            return "identifier"
        if is_numeric_dtype(series.dtype) and not is_bool_dtype(series):
            return "measure"
        if len(series) >= 20 and unique_ratio >= 0.98 and not is_bool_dtype(series):
            return "identifier"
        unique_count = int(series.nunique(dropna=True))
        if is_bool_dtype(series.dtype) or unique_count <= min(50, max(10, len(series) // 5)):
            return "dimension"
        return "text"



    @staticmethod
    def _find_joins(left: pd.DataFrame, right: pd.DataFrame) -> List[JoinCandidate]:
        """Find joins."""
        joins = []
        for left_column in left.columns:
            left_series = left[left_column].dropna()
            if len(left_series) == 0:
                continue

            left_values = {str(v).strip().casefold() for v in left_series.unique()}
            if not left_values:
                continue

            for right_column in right.columns:
                right_series = right[right_column].dropna()
                if len(right_series) == 0:
                    continue
                right_values = {
                    str(v).strip().casefold()
                    for v in right_series.unique()
                }
                if not right_values:
                    continue


                overlap = len(left_values & right_values)
                coverage = overlap / len(left_values) if left_values else 0.0

                same_name = str(left_column).casefold() == str(right_column).casefold()
                if coverage < 0.5 and not same_name:
                    continue

                # cardinality
                left_unique = left_series.is_unique
                right_unique = right_series.is_unique
                if left_unique and right_unique:
                    cardinality = "one_to_one"
                elif right_unique:
                    cardinality = "many_to_one"
                elif left_unique:
                    cardinality = "one_to_many"
                else:
                    cardinality = "many_to_many"

                joins.append(
                    JoinCandidate(
                        left_source="main",
                        left_column=str(left_column),
                        right_source="user",
                        right_column=str(right_column),
                        coverage=float(coverage),
                        expected_cardinality=cardinality,
                    )
                )

        joins.sort(key=lambda j: (-j.coverage, j.left_column, j.right_column))
        return joins[:12]
