#!/usr/bin/env python3
"""Build the initial ontology layer (ontology_v0) for the BIRD formula_1 database.

Reads the frozen construction split (data/minidev/train/formula_1.json) and the
SQLite schema/values as evidence, then publishes a schema-conformant semantic
layer under <bird>/.evoontology/formula_1/.

Run from the bird directory:
    python scripts/build_formula_1_ontology_layer.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BIRD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIRD_DIR.parent.parent))  # project root -> evoontology package

from evoontology import EvolutionTrigger, SemanticStore, ensure_workspace, save_project
from evoontology.validate import validate

DB_ID = "formula_1"
VERSION = "ontology_v0"
DATABASE_SOURCE = "formula_1"
TS = "2026-08-19T00:00:00Z"

WORKSPACE = BIRD_DIR / ".evoontology" / DB_ID

# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

EVIDENCE = [
    {
        "id": "evidence_schema_circuits",
        "source": DATABASE_SOURCE,
        "query": "PRAGMA table_info(circuits); SELECT circuitId, name, location, country, lat, lng FROM circuits LIMIT 5",
        "result": "circuits has circuitId, circuitRef, name, location, country, lat(REAL), lng(REAL); e.g. Sepang International Circuit, Kuala Lumpur, Malaysia, lat 2.76083 lng 101.738",
        "validation_method": "schema + sample value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_schema_drivers",
        "source": DATABASE_SOURCE,
        "query": "PRAGMA table_info(drivers); SELECT driverId, driverRef, code, forename, surname, dob, nationality FROM drivers LIMIT 5",
        "result": "drivers has driverId, driverRef, number, code, forename, surname, dob(DATE), nationality; e.g. driverId 1 'hamilton' code 'HAM' Lewis Hamilton 1985-01-07 British",
        "validation_method": "schema + sample value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_schema_constructors",
        "source": DATABASE_SOURCE,
        "query": "PRAGMA table_info(constructors); SELECT constructorId, constructorRef, name, nationality FROM constructors LIMIT 5",
        "result": "constructors has constructorId, constructorRef, name, nationality, url",
        "validation_method": "schema verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_schema_races",
        "source": DATABASE_SOURCE,
        "query": "PRAGMA table_info(races); SELECT raceId, year, round, circuitId, name, date FROM races LIMIT 5",
        "result": "races has raceId, year, round, circuitId, name, date, time, url; raceId 1 = 2009 Australian Grand Prix 2009-03-29",
        "validation_method": "schema + sample value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_status_enum",
        "source": DATABASE_SOURCE,
        "query": "SELECT statusId, status FROM status ORDER BY statusId",
        "result": "statusId 1='Finished', 2='Disqualified', 3='Accident', 4='Collision', 5='Engine', 11-19/45/...='+N Lap(s)', 31='Retired', 62='Not classified', 81='Did not qualify', 96='Excluded', 97='Did not prequalify', 104='Fatal accident'",
        "validation_method": "enumeration value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_race_id_reference",
        "source": DATABASE_SOURCE,
        "query": "SELECT raceId, year, name FROM races WHERE raceId IN (45,291,354,853,872,901,903) ORDER BY raceId",
        "result": "each referenced 'race No. N' in the workload resolves to a valid races.raceId (45=2007 European GP, 291=1992 Brazilian GP, 354=2010 Brazilian GP, 853=2011 Italian GP, 872=2012 Italian GP, 901=2014 Malaysian GP, 903=2014 Chinese GP)",
        "validation_method": "workload term to value cross-check",
        "timestamp": TS,
    },
    {
        "id": "evidence_position_text_enum",
        "source": DATABASE_SOURCE,
        "query": "SELECT DISTINCT positionText FROM results ORDER BY positionText",
        "result": "positionText values are numeric strings '1'..'33' plus letter codes D,E,F,N,R,W (e.g. R=Retired, D=Disqualified, E=Excluded, N=Not classified, W=Withdrew)",
        "validation_method": "enumeration value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_driver_code_sparse",
        "source": DATABASE_SOURCE,
        "query": "SELECT COUNT(*) FROM drivers WHERE code IS NULL OR code=''; SELECT COUNT(*) FROM drivers",
        "result": "757 of 840 drivers have NULL/empty code; code is a 3-letter abbreviation (e.g. HAM, ROS, ALO)",
        "validation_method": "null-ratio + sample verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_qualifying_sparse",
        "source": DATABASE_SOURCE,
        "query": "SELECT COUNT(*) FROM qualifying WHERE q2 IS NULL; SELECT COUNT(*) FROM qualifying WHERE q3 IS NULL; SELECT COUNT(*) FROM qualifying",
        "result": "q2 NULL for 3807/7397 rows; q3 NULL for 5251/7397 rows (only top-15 of Q1 record q2, only top-10 of Q2 record q3)",
        "validation_method": "null-ratio verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_fastest_lap_speed_unit",
        "source": DATABASE_SOURCE,
        "query": "SELECT raceId, driverId, fastestLapSpeed, fastestLapTime, rank FROM results WHERE fastestLapSpeed IS NOT NULL LIMIT 5",
        "result": "fastestLapSpeed stored as TEXT but numeric km/h, e.g. '218.300'; rank is a per-race ranking by fastest lap speed (1 = fastest)",
        "validation_method": "sample value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_finish_time_format",
        "source": DATABASE_SOURCE,
        "query": "SELECT resultId, raceId, position, time, milliseconds, statusId FROM results LIMIT 5",
        "result": "results.time is TEXT: winner '1:34:50.616' (MM:SS.mmm), others '+5.478' (offset seconds); results.milliseconds is integer actual finishing time (e.g. 5690616)",
        "validation_method": "sample value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_lap_time_format",
        "source": DATABASE_SOURCE,
        "query": "SELECT raceId, driverId, lap, time, milliseconds FROM lapTimes LIMIT 5",
        "result": "lapTimes.time is TEXT 'M:SS.mmm' (e.g. '1:49.088'); lapTimes.milliseconds is integer (e.g. 109088) usable for numeric comparison",
        "validation_method": "sample value verification",
        "timestamp": TS,
    },
    {
        "id": "evidence_points_grain",
        "source": DATABASE_SOURCE,
        "query": "SELECT points FROM results LIMIT 3; SELECT points FROM driverStandings LIMIT 3; SELECT points FROM constructorResults LIMIT 3",
        "result": "results.points = per-driver per-race points (10,8,6,...); driverStandings.points = cumulative season points; constructorResults.points = per-constructor per-race points",
        "validation_method": "grain comparison across tables",
        "timestamp": TS,
    },
]

# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------

TERMS = [
    {"id": "circuit", "type": "entity", "name": "Circuit",
     "definition": "A Formula 1 racing circuit or venue.",
     "scope": "Race venue and geographic analysis",
     "aliases": ["track", "venue", "grand prix circuit", "circuit"],
     "evidence": ["evidence_schema_circuits"]},
    {"id": "driver", "type": "entity", "name": "Driver",
     "definition": "A Formula 1 driver (racer).",
     "scope": "Driver performance and biography analysis",
     "aliases": ["racer", "pilot", "driver"],
     "evidence": ["evidence_schema_drivers"]},
    {"id": "constructor", "type": "entity", "name": "Constructor",
     "definition": "A Formula 1 constructor (team).",
     "scope": "Team/constructor analysis",
     "aliases": ["team", "constructor"],
     "evidence": ["evidence_schema_constructors"]},
    {"id": "race", "type": "entity", "name": "Race",
     "definition": "A Formula 1 Grand Prix race event, uniquely identified by raceId.",
     "scope": "Race event analysis",
     "aliases": ["grand prix", "event", "race event", "race"],
     "evidence": ["evidence_schema_races", "evidence_race_id_reference"]},
    {"id": "race_result", "type": "entity", "name": "Race Result",
     "definition": "A single driver's result in a race (the central fact record).",
     "scope": "Per-driver per-race outcome analysis",
     "aliases": ["result", "finish record", "race result"],
     "evidence": ["evidence_finish_time_format", "evidence_position_text_enum"]},
    {"id": "status", "type": "entity", "name": "Status",
     "definition": "The finishing status of a race result (Finished, Accident, +N Laps, etc.).",
     "scope": "Finish/classification interpretation",
     "aliases": ["finish status", "result status", "status"],
     "evidence": ["evidence_status_enum"]},
    {"id": "qualifying_session", "type": "entity", "name": "Qualifying Session",
     "definition": "A driver's qualifying performance in a race.",
     "scope": "Qualifying analysis (Q1/Q2/Q3)",
     "aliases": ["qualifying", "qualify"],
     "evidence": ["evidence_qualifying_sparse"]},
    {"id": "season", "type": "entity", "name": "Season",
     "definition": "A Formula 1 season (calendar year of the championship).",
     "scope": "Season-level analysis",
     "aliases": ["season", "championship year"],
     "evidence": ["evidence_schema_races"]},
    {"id": "driver_nationality", "type": "dimension", "name": "Driver Nationality",
     "definition": "The nationality of a driver.",
     "scope": "Driver demographic analysis",
     "aliases": ["nationality", "driver nationality", "country of driver"],
     "evidence": ["evidence_schema_drivers"]},
    {"id": "circuit_country", "type": "dimension", "name": "Circuit Country",
     "definition": "The country where a circuit is located.",
     "scope": "Geographic circuit analysis",
     "aliases": ["country", "circuit country"],
     "evidence": ["evidence_schema_circuits"]},
    {"id": "circuit_location", "type": "dimension", "name": "Circuit Location",
     "definition": "The city/town where a circuit is located.",
     "scope": "Geographic circuit analysis",
     "aliases": ["location", "city", "town"],
     "evidence": ["evidence_schema_circuits"]},
    {"id": "race_year", "type": "dimension", "name": "Race Year",
     "definition": "The calendar year in which a race took place.",
     "scope": "Temporal race analysis",
     "aliases": ["year", "race year"],
     "evidence": ["evidence_schema_races"]},
    {"id": "finishing_position", "type": "dimension", "name": "Finishing Position",
     "definition": "The finishing position (rank) of a driver in a race.",
     "scope": "Race outcome analysis",
     "aliases": ["position", "finish position", "rank", "placed", "ranking"],
     "evidence": ["evidence_position_text_enum"]},
    {"id": "standings_position", "type": "dimension", "name": "Standings Position",
     "definition": "A driver's or constructor's position in the championship standings after a race.",
     "scope": "Championship standings analysis",
     "aliases": ["standings", "championship position", "standing"],
     "evidence": ["evidence_points_grain"]},
    {"id": "driver_code", "type": "category", "name": "Driver Code",
     "definition": "The 3-letter abbreviated code of a driver (e.g. HAM).",
     "scope": "Driver identification",
     "aliases": ["code", "abbreviated code", "abbreviation"],
     "evidence": ["evidence_driver_code_sparse"]},
    {"id": "coordinates", "type": "dimension", "name": "Coordinates",
     "definition": "Geographic coordinates (latitude, longitude) of a circuit.",
     "scope": "Geographic circuit analysis",
     "aliases": ["coordinates", "latitude", "longitude", "lat", "lng"],
     "evidence": ["evidence_schema_circuits"]},
    {"id": "points", "type": "metric", "name": "Points",
     "definition": "Championship points awarded to a driver or constructor.",
     "scope": "Scoring and standings analysis",
     "aliases": ["points", "score", "point scores"],
     "evidence": ["evidence_points_grain"]},
    {"id": "wins", "type": "metric", "name": "Wins",
     "definition": "The number of race wins accumulated by a driver or constructor.",
     "scope": "Standings analysis",
     "aliases": ["wins", "winning", "victories"],
     "evidence": ["evidence_points_grain"]},
    {"id": "lap_time", "type": "metric", "name": "Lap Time",
     "definition": "The time taken to complete one lap of a race.",
     "scope": "Lap-level performance analysis",
     "aliases": ["lap time", "laptime", "lap"],
     "evidence": ["evidence_lap_time_format"]},
    {"id": "fastest_lap_time", "type": "metric", "name": "Fastest Lap Time",
     "definition": "The fastest (best) lap time recorded by a driver in a race.",
     "scope": "Lap performance analysis",
     "aliases": ["fastest lap time", "best lap time", "fastest lap"],
     "evidence": ["evidence_fastest_lap_speed_unit"]},
    {"id": "fastest_lap_speed", "type": "metric", "name": "Fastest Lap Speed",
     "definition": "The fastest lap speed, in kilometres per hour.",
     "scope": "Lap performance analysis",
     "aliases": ["fastest lap speed", "lap speed", "speed"],
     "evidence": ["evidence_fastest_lap_speed_unit"]},
    {"id": "finish_time", "type": "metric", "name": "Finish Time",
     "definition": "The total finishing time of a driver in a race.",
     "scope": "Race outcome analysis",
     "aliases": ["finish time", "finishing time", "race time"],
     "evidence": ["evidence_finish_time_format"]},
    {"id": "driver_age", "type": "metric", "name": "Driver Age",
     "definition": "The age of a driver, derived from their date of birth.",
     "scope": "Driver demographic analysis",
     "aliases": ["age", "oldest", "youngest"],
     "evidence": ["evidence_schema_drivers"]},
    {"id": "driver_full_name", "type": "concept", "name": "Driver Full Name",
     "definition": "The full name of a driver, formed from forename and surname.",
     "scope": "Driver identification",
     "aliases": ["full name", "name", "driver name"],
     "evidence": ["evidence_schema_drivers"]},
    {"id": "qualifying_time", "type": "metric", "name": "Qualifying Time",
     "definition": "A driver's qualifying lap time in a qualifying session (Q1, Q2, or Q3).",
     "scope": "Qualifying analysis",
     "aliases": ["qualifying time", "Q1", "Q2", "Q3", "qualifying result"],
     "evidence": ["evidence_qualifying_sparse"]},
    {"id": "champion", "type": "concept", "name": "Champion",
     "definition": "The driver who won a season's championship (first in the final standings).",
     "scope": "Championship outcome analysis",
     "aliases": ["champion", "winner", "championship winner"],
     "evidence": ["evidence_points_grain"]},
]

# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

def _m(mid, term_id, table, column, **kw):
    rec = {
        "id": mid,
        "term_id": term_id,
        "database_source": DATABASE_SOURCE,
        "table": table,
        "column": column,
        "semantic_filter": kw.get("semantic_filter", ""),
        "aggregation_semantics": kw.get("aggregation_semantics", ""),
        "grain": kw.get("grain", ""),
        "validation": kw.get("validation", ""),
        "evidence_refs": kw.get("evidence_refs", []),
    }
    return rec

MAPPINGS = [
    _m("mapping_circuit_id", "circuit", "circuits", "circuitId", grain="one circuit"),
    _m("mapping_circuit_name", "circuit", "circuits", "name", grain="one circuit"),
    _m("mapping_driver_id", "driver", "drivers", "driverId", grain="one driver"),
    _m("mapping_driver_ref", "driver", "drivers", "driverRef", grain="one driver"),
    _m("mapping_constructor_id", "constructor", "constructors", "constructorId", grain="one constructor"),
    _m("mapping_constructor_name", "constructor", "constructors", "name", grain="one constructor"),
    _m("mapping_race_id", "race", "races", "raceId", grain="one race"),
    _m("mapping_race_name", "race", "races", "name", grain="one race"),
    _m("mapping_race_date", "race", "races", "date", grain="one race"),
    _m("mapping_result_id", "race_result", "results", "resultId", grain="one race result"),
    _m("mapping_status_id", "status", "status", "statusId", grain="one status"),
    _m("mapping_status_name", "status", "status", "status", grain="one status"),
    _m("mapping_qualifying_id", "qualifying_session", "qualifying", "qualifyId", grain="one qualifying session"),
    _m("mapping_qualifying_position", "qualifying_session", "qualifying", "position", grain="per driver per race"),
    _m("mapping_season_year", "season", "seasons", "year", grain="one season"),
    _m("mapping_season_url", "season", "seasons", "url", grain="one season"),
    _m("mapping_driver_nationality", "driver_nationality", "drivers", "nationality", grain="per driver"),
    _m("mapping_circuit_country", "circuit_country", "circuits", "country", grain="per circuit"),
    _m("mapping_circuit_location", "circuit_location", "circuits", "location", grain="per circuit"),
    _m("mapping_race_year", "race_year", "races", "year", grain="per race"),
    _m("mapping_finishing_position", "finishing_position", "results", "position", grain="per driver per race"),
    _m("mapping_position_order", "finishing_position", "results", "positionOrder", grain="per driver per race"),
    _m("mapping_driver_standings_position", "standings_position", "driverStandings", "position", grain="per driver per race"),
    _m("mapping_constructor_standings_position", "standings_position", "constructorStandings", "position", grain="per constructor per race"),
    _m("mapping_driver_code", "driver_code", "drivers", "code", grain="per driver", evidence_refs=["evidence_driver_code_sparse"]),
    _m("mapping_lat", "coordinates", "circuits", "lat", grain="per circuit"),
    _m("mapping_lng", "coordinates", "circuits", "lng", grain="per circuit"),
    _m("mapping_points_result", "points", "results", "points", grain="per driver per race",
       aggregation_semantics="sum of points across drivers or races", evidence_refs=["evidence_points_grain"]),
    _m("mapping_points_driver_standing", "points", "driverStandings", "points", grain="cumulative season points for a driver",
       aggregation_semantics="cumulative points, not summed across races", evidence_refs=["evidence_points_grain"]),
    _m("mapping_points_constructor_standing", "points", "constructorStandings", "points", grain="cumulative season points for a constructor",
       aggregation_semantics="cumulative points, not summed across races", evidence_refs=["evidence_points_grain"]),
    _m("mapping_points_constructor_result", "points", "constructorResults", "points", grain="per constructor per race",
       aggregation_semantics="sum of points across constructors or races", evidence_refs=["evidence_points_grain"]),
    _m("mapping_wins_driver", "wins", "driverStandings", "wins", grain="cumulative season wins for a driver"),
    _m("mapping_wins_constructor", "wins", "constructorStandings", "wins", grain="cumulative season wins for a constructor"),
    _m("mapping_lap_time", "lap_time", "lapTimes", "time", grain="per driver per lap per race", evidence_refs=["evidence_lap_time_format"]),
    _m("mapping_lap_time_ms", "lap_time", "lapTimes", "milliseconds", grain="per driver per lap per race",
       aggregation_semantics="minimum lap time gives the fastest lap", evidence_refs=["evidence_lap_time_format"]),
    _m("mapping_fastest_lap_time", "fastest_lap_time", "results", "fastestLapTime", grain="per driver per race",
       aggregation_semantics="minimum fastest lap time across drivers", evidence_refs=["evidence_fastest_lap_speed_unit"]),
    _m("mapping_fastest_lap_speed", "fastest_lap_speed", "results", "fastestLapSpeed", grain="per driver per race",
       aggregation_semantics="maximum fastest lap speed across drivers", evidence_refs=["evidence_fastest_lap_speed_unit"]),
    _m("mapping_finish_time", "finish_time", "results", "time", grain="per driver per race", evidence_refs=["evidence_finish_time_format"]),
    _m("mapping_finish_time_ms", "finish_time", "results", "milliseconds", grain="per driver per race",
       aggregation_semantics="minimum finishing time identifies the winner", evidence_refs=["evidence_finish_time_format"]),
    _m("mapping_driver_age", "driver_age", "drivers", "dob", grain="per driver"),
    _m("mapping_full_name_forename", "driver_full_name", "drivers", "forename", grain="per driver"),
    _m("mapping_full_name_surname", "driver_full_name", "drivers", "surname", grain="per driver"),
    _m("mapping_qual_time_q1", "qualifying_time", "qualifying", "q1", semantic_filter="qualifying session Q1", grain="per driver per race"),
    _m("mapping_qual_time_q2", "qualifying_time", "qualifying", "q2", semantic_filter="qualifying session Q2", grain="per driver per race"),
    _m("mapping_qual_time_q3", "qualifying_time", "qualifying", "q3", semantic_filter="qualifying session Q3", grain="per driver per race"),
    _m("mapping_champion_position", "champion", "driverStandings", "position", semantic_filter="season champion is the driver with standings position 1",
       grain="per season", evidence_refs=["evidence_points_grain"]),
]

# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------

RELATIONS = [
    {"id": "rel_race_circuit", "source": "race", "relation_type": "association", "target": "circuit",
     "connection_condition": "races.circuitId references circuits.circuitId",
     "description": "A race is held at a circuit; the circuit is the venue for the race event.",
     "evidence": ["evidence_schema_races", "evidence_schema_circuits"]},
    {"id": "rel_result_race", "source": "race_result", "relation_type": "composition", "target": "race",
     "connection_condition": "results.raceId references races.raceId",
     "description": "A race result is a per-driver record within a single race.",
     "evidence": ["evidence_schema_races"]},
    {"id": "rel_result_driver", "source": "race_result", "relation_type": "composition", "target": "driver",
     "connection_condition": "results.driverId references drivers.driverId",
     "description": "A race result records the outcome for a specific driver.",
     "evidence": ["evidence_schema_drivers"]},
    {"id": "rel_result_constructor", "source": "race_result", "relation_type": "composition", "target": "constructor",
     "connection_condition": "results.constructorId references constructors.constructorId",
     "description": "A race result is attributed to a constructor team.",
     "evidence": ["evidence_schema_constructors"]},
    {"id": "rel_result_status", "source": "race_result", "relation_type": "association", "target": "status",
     "connection_condition": "results.statusId references status.statusId",
     "description": "A race result carries a finish status describing how the race ended.",
     "evidence": ["evidence_status_enum"]},
    {"id": "rel_qualifying_race", "source": "qualifying_session", "relation_type": "composition", "target": "race",
     "connection_condition": "qualifying.raceId references races.raceId",
     "description": "A qualifying session is part of a race weekend.",
     "evidence": ["evidence_qualifying_sparse"]},
    {"id": "rel_qualifying_driver", "source": "qualifying_session", "relation_type": "composition", "target": "driver",
     "connection_condition": "qualifying.driverId references drivers.driverId",
     "description": "A qualifying session records the qualifying performance of a driver.",
     "evidence": ["evidence_schema_drivers"]},
    {"id": "rel_driver_nationality", "source": "driver", "relation_type": "association", "target": "driver_nationality",
     "connection_condition": "driver nationality is a text attribute of drivers, with no separate country table",
     "description": "A driver has a nationality recorded as a text value.",
     "evidence": ["evidence_schema_drivers"]},
    {"id": "rel_circuit_country", "source": "circuit", "relation_type": "association", "target": "circuit_country",
     "connection_condition": "circuit country is a text attribute of circuits, with no separate country table",
     "description": "A circuit is located in a country recorded as a text value.",
     "evidence": ["evidence_schema_circuits"]},
    {"id": "rel_race_year_equiv", "source": "race_year", "relation_type": "equivalence", "target": "season",
     "connection_condition": "races.year and seasons.year share the same value domain (calendar year)",
     "description": "The race year and the season year refer to the same calendar year.",
     "evidence": ["evidence_schema_races"]},
    {"id": "rel_full_name", "source": "driver", "relation_type": "derivation", "target": "driver_full_name",
     "connection_condition": "full name = forename || ' ' || surname",
     "description": "A driver's full name is derived from their forename and surname.",
     "evidence": ["evidence_schema_drivers"]},
    {"id": "rel_age", "source": "driver", "relation_type": "derivation", "target": "driver_age",
     "connection_condition": "age = race date minus date of birth",
     "description": "A driver's age is derived from their date of birth relative to a reference date.",
     "evidence": ["evidence_schema_drivers"]},
    {"id": "rel_fastest_lap_time", "source": "lap_time", "relation_type": "derivation", "target": "fastest_lap_time",
     "connection_condition": "fastest lap time = minimum lap time per driver per race",
     "description": "A driver's fastest lap time is the minimum of their lap times in a race.",
     "evidence": ["evidence_fastest_lap_speed_unit"]},
    {"id": "rel_champion", "source": "driver", "relation_type": "derivation", "target": "champion",
     "connection_condition": "champion = driver whose final standings position is 1",
     "description": "The champion is the driver who finishes a season first in the standings.",
     "evidence": ["evidence_points_grain"]},
]

# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

CONSTRAINTS = [
    {"id": "constraint_race_no_means_raceid", "target": "race", "constraint_type": "business_rule",
     "trigger_keywords": ["race no", "race number", "race no.", "no.", "the race", "race 291"],
     "severity": "block",
     "scope": "Questions referencing a numbered race",
     "confidence": "high",
     "description": "'race No. N' / 'race number N' refers to the races.raceId column, not the round number.",
     "evidence": ["evidence_race_id_reference"]},
    {"id": "constraint_status_finished", "target": "status", "constraint_type": "enum_semantics",
     "trigger_keywords": ["finished", "finish", "did not finish", "retired", "completed the race", "finish status"],
     "severity": "block",
     "scope": "Filtering drivers by whether they finished a race",
     "confidence": "high",
     "description": "status 'Finished' (statusId 1) means the driver finished the race. '+N Lap(s)' statuses mean classified but lapped. Mechanical/accident statuses (Accident, Collision, Engine, Retired, etc.) mean the driver did not finish.",
     "evidence": ["evidence_status_enum"]},
    {"id": "constraint_finish_time_format", "target": "finish_time", "constraint_type": "unit",
     "trigger_keywords": ["finish time", "finishing time", "race time", "milliseconds", "champion time", "time of"],
     "severity": "block",
     "scope": "Ordering or comparing finishing times",
     "confidence": "high",
     "description": "results.time is text: the winner shows 'MM:SS.mmm', other finishers show '+SS.mmm' (offset behind the winner). results.milliseconds is the integer actual finishing time and should be used for numeric comparison/ordering.",
     "evidence": ["evidence_finish_time_format"]},
    {"id": "constraint_fastest_lap_speed_unit", "target": "fastest_lap_speed", "constraint_type": "unit",
     "trigger_keywords": ["fastest lap speed", "lap speed", "km/h", "speed"],
     "severity": "warning",
     "scope": "Comparing or aggregating fastest lap speeds",
     "confidence": "high",
     "description": "results.fastestLapSpeed is stored as text but represents a numeric value in kilometres per hour; cast to numeric for comparisons and averages.",
     "evidence": ["evidence_fastest_lap_speed_unit"]},
    {"id": "constraint_position_semantics", "target": "finishing_position", "constraint_type": "enum_semantics",
     "trigger_keywords": ["position", "ranked", "rank", "placed", "finished", "position order", "first", "second", "winner"],
     "severity": "block",
     "scope": "Interpreting finishing position and rank",
     "confidence": "high",
     "description": "results.position is the integer finishing position (NULL for non-finishers); positionOrder is a total ordering including non-finishers; positionText is a text form with letter codes for non-finishes (R=Retired, D=Disqualified, E=Excluded, N=Not classified, W=Withdrew).",
     "evidence": ["evidence_position_text_enum"]},
    {"id": "constraint_qualifying_sparse", "target": "qualifying_time", "constraint_type": "data_quality",
     "trigger_keywords": ["Q1", "Q2", "Q3", "qualifying", "qualifying time"],
     "severity": "warning",
     "scope": "Interpreting qualifying times",
     "confidence": "high",
     "description": "qualifying.q2 is recorded only for the top-15 of Q1 and is NULL otherwise; qualifying.q3 is recorded only for the top-10 of Q2 and is NULL otherwise. NULL means the driver did not advance to that session.",
     "evidence": ["evidence_qualifying_sparse"]},
    {"id": "constraint_driver_code_sparse", "target": "driver_code", "constraint_type": "data_quality",
     "trigger_keywords": ["abbreviated code", "code", "abbreviation", "driver code"],
     "severity": "info",
     "scope": "Looking up a driver by code",
     "confidence": "high",
     "description": "drivers.code is a 3-letter abbreviation (e.g. HAM) and is NULL/empty for many drivers (757 of 840); do not assume every driver has a code.",
     "evidence": ["evidence_driver_code_sparse"]},
    {"id": "constraint_coordinates", "target": "coordinates", "constraint_type": "unit",
     "trigger_keywords": ["coordinates", "latitude", "longitude", "location coordinates", "lat", "lng"],
     "severity": "info",
     "scope": "Reporting circuit coordinates",
     "confidence": "high",
     "description": "A circuit's coordinates are the pair (circuits.lat, circuits.lng), both REAL values.",
     "evidence": ["evidence_schema_circuits"]},
    {"id": "constraint_champion_position", "target": "champion", "constraint_type": "business_rule",
     "trigger_keywords": ["champion", "championship", "winner", "championship winner"],
     "severity": "warning",
     "scope": "Identifying a season champion",
     "confidence": "medium",
     "description": "A season champion is the driver with standings position 1 in driverStandings.",
     "evidence": ["evidence_points_grain"]},
    {"id": "constraint_lap_time_ms", "target": "lap_time", "constraint_type": "unit",
     "trigger_keywords": ["lap time", "laptime", "lap", "less than", "faster than", "02:00"],
     "severity": "warning",
     "scope": "Comparing or filtering lap times",
     "confidence": "high",
     "description": "lapTimes.time is text in 'M:SS.mmm' format; lapTimes.milliseconds is an integer and should be used for numeric comparisons (e.g. a lap time 'less than 02:00.00').",
     "evidence": ["evidence_lap_time_format"]},
]

# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

PROJECT = {
    "schema_version": 1,
    "mode": "fixed_split",
    "data_source": {
        "type": "sqlite",
        "db_id": DB_ID,
        "path": "data/mini_dev_data/dev_databases/formula_1/formula_1.sqlite",
    },
    "workload_source": {
        "dataset": "minidev",
        "path": "data/minidev/train/formula_1.json",
        "construction_split": "train",
        "split_seed": 42,
    },
    "evaluation": {"type": "execution_based", "dataset": "minidev"},
    "boundary": {
        "strategy": "fixed_split",
        "db_id": DB_ID,
        "split_seed": 42,
        "train_count": 33,
        "test_count": 33,
    },
}

BUILD_METADATA = {
    "version": VERSION,
    "db_id": DB_ID,
    "construction_split": "minidev/train",
    "split_seed": 42,
    "train_question_count": 33,
    "built_at": TS,
    "counts": {
        "terms": len(TERMS),
        "mappings": len(MAPPINGS),
        "relations": len(RELATIONS),
        "constraints": len(CONSTRAINTS),
        "evidence": len(EVIDENCE),
    },
    "rejected_candidates": [
        "No separate country/nationality entity: drivers.nationality and circuits.country are plain text columns, so nationality is modelled as a dimension with an association relation rather than a country entity",
        "race-to-circuit is 'association' not 'composition': a race is 'held at' a circuit, not a component of it",
        "No hierarchy relations: no genuine is-a (type/subtype) relationship exists among the workload concepts",
    ],
    "known_limitations": [
        "Driver age has no stored column and must be derived from drivers.dob against a reference date",
        "drivers.code is missing for most drivers (757/840)",
        "qualifying.q2/q3 are sparsely populated by design (top-15 / top-10 only)",
        "Fastest lap speed and lap/finish times are stored as text and require numeric casting for comparison",
    ],
}


def main() -> int:
    workspace = ensure_workspace(WORKSPACE)
    save_project(PROJECT, workspace)

    records = {
        "terms": TERMS,
        "mappings": MAPPINGS,
        "relations": RELATIONS,
        "constraints": CONSTRAINTS,
        "evidence": EVIDENCE,
    }
    SemanticStore.save_version(workspace, VERSION, records)
    SemanticStore.set_active(workspace, VERSION)
    EvolutionTrigger(str(workspace)).initialize()

    (workspace / "build_metadata.json").write_text(
        json.dumps(BUILD_METADATA, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = validate(str(workspace))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
