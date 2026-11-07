"""Check synthetic fixture integrity, not production system behavior."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))

def validate(directory, report_path, reproduction=None):
    manifest=read_json(directory/"manifest.json")
    checks=[]
    def note(name, details):
        checks.append({"name":name,"ok":True,"details":details})
    assert manifest["is_simulated"] is True
    for name, digest in manifest["files_sha256"].items():
        file=directory/name
        assert file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest()==digest, name
    note("file_integrity",len(manifest["files_sha256"]))

    org=read_json(directory/"organization.json")
    assert len(org["employees"])==30 and len(org["teams"])==3 and len(org["leaders"])==3
    employee_by_id={e["employee_id"]:e for e in org["employees"]}
    team_ids={t["team_id"] for t in org["teams"]}
    account_ids=set(employee_by_id)|{l["account_id"] for l in org["leaders"]}|{org["administrator"]["account_id"]}
    assert len(account_ids)==34
    assert Counter(e["initial_team_id"] for e in org["employees"])==Counter({"T01":10,"T02":10,"T03":10})
    assert all(e["is_simulated"] for e in org["employees"])
    with (directory/"employees.csv").open(encoding="utf-8",newline="") as stream:
        csv_employees=list(csv.DictReader(stream))
    assert {e["employee_id"] for e in csv_employees}==set(employee_by_id) and len(csv_employees)==30
    with (directory/"devices.csv").open(encoding="utf-8",newline="") as stream:
        devices=list(csv.DictReader(stream))
    device_ids={d["device_id"] for d in devices}
    assert len(device_ids)==len(devices)==32
    assert all(e["device_id"] in device_ids for e in org["employees"])
    note("organization_and_device_relations",{"employees":30,"leaders":3,"admins":1,"teams":3,"logical_devices":32})

    areas=read_json(directory/"areas.json")
    assert areas["coordinate_system"]=="synthetic_local_metre" and not areas["real_site"]
    versions={v["version_id"]:v for v in areas["area_versions"]}
    assert len(versions)==5
    for version in versions.values():
        polygon=version["polygon_xy_m"]
        assert polygon[0]==polygon[-1] and len(polygon)>=4
        if version["effective_to"]:
            assert datetime.fromisoformat(version["effective_from"])<datetime.fromisoformat(version["effective_to"])
    note("area_versions_and_coordinate_label",{"versions":len(versions),"not_geographic_coordinates":True})

    file_results=[]
    for count in manifest["position_counts"]:
        ids=set(); previous={}; by_employee=Counter(); session_by_employee={}; first=None; last=None
        with (directory/f"positions_{count}.csv").open(encoding="utf-8",newline="") as stream:
            for row in csv.DictReader(stream):
                point_id=row["observation_id"]; employee_id=row["employee_id"]
                assert point_id not in ids; ids.add(point_id)
                assert employee_id in employee_by_id and row["is_simulated"]=="True"
                employee=employee_by_id[employee_id]
                assert row["initial_team_id"]==employee["initial_team_id"]
                assert row["device_id"]==employee["device_id"]
                assert row["assigned_area_id"]==employee["initial_area_id"]
                observed=datetime.fromisoformat(row["observed_at"])
                received=datetime.fromisoformat(row["received_at"])
                assert observed.tzinfo is not None and received>=observed
                if employee_id in previous:
                    assert (observed-previous[employee_id]).total_seconds()==5
                previous[employee_id]=observed
                session_by_employee.setdefault(employee_id,row["session_id"])
                assert session_by_employee[employee_id]==row["session_id"]
                version=versions[row["area_version_id"]]
                assert version["area_id"]==row["assigned_area_id"]
                assert observed>=datetime.fromisoformat(version["effective_from"])
                assert not version["effective_to"] or observed<datetime.fromisoformat(version["effective_to"])
                x,y,u=map(float,(row["x_m"],row["y_m"],row["uncertainty_m"]))
                assert all(math.isfinite(v) for v in (x,y,u)) and u>=0
                polygon=version["polygon_xy_m"]
                assert min(p[0] for p in polygon)<=x<=max(p[0] for p in polygon)
                assert min(p[1] for p in polygon)<=y<=max(p[1] for p in polygon)
                by_employee[employee_id]+=1
                first=observed if first is None else min(first,observed)
                last=observed if last is None else max(last,observed)
        assert len(ids)==count and len(by_employee)==30 and len(set(session_by_employee.values()))==30
        assert max(by_employee.values())-min(by_employee.values())<=1
        file_results.append({"file":f"positions_{count}.csv","rows":count,"logical_streams":30,"first_observed_at":first.isoformat(),"last_observed_at":last.isoformat(),"min_points_per_employee":min(by_employee.values()),"max_points_per_employee":max(by_employee.values())})
    note("bulk_counts_times_coordinates_versions",file_results)

    sizes=sorted(manifest["position_counts"])
    with (directory/f"positions_{sizes[0]}.csv").open("rb") as small, (directory/f"positions_{sizes[-1]}.csv").open("rb") as large:
        for line in small:
            assert line==large.readline(),"The smaller dataset is not a prefix"
    note("nested_dataset_prefix",{"smaller":sizes[0],"larger":sizes[-1],"additive_independent_rows":False})

    casefile=read_json(directory/"scenarios.json")
    cases=casefile["scenarios"]
    assert len(cases)==12 and len({c["scenario_id"] for c in cases})==12
    for case in cases:
        assert case["is_simulated"] and case["expectation_status"]=="acceptance_fixture_not_executed"
        assert case["rule_refs"] and all(r in manifest["source_rule_ids"] for r in case["rule_refs"])
        assert case["expected"] and case["actions"]
        last_action=None
        for action in case["actions"]:
            time=datetime.fromisoformat(action["at"])
            assert last_action is None or time>=last_action
            assert action["actor_id"] in account_ids|{"system"}
            for key in ("employee_id","leader_id"):
                if key in action: assert action[key] in account_ids
            for key in ("team_id","from_team","to_team"):
                if key in action: assert action[key] in team_ids
            for point in action.get("observations",[]):
                assert point["is_simulated"] and datetime.fromisoformat(point["received_at"])>=datetime.fromisoformat(point["observed_at"])
            last_action=time
    note("scenario_structure_references_and_chronology",{"cases":12,"application_assertions_executed":False})

    if reproduction:
        other=read_json(reproduction/"manifest.json")
        assert manifest==other,"Reproduced manifest differs"
        for name,digest in manifest["files_sha256"].items():
            assert hashlib.sha256((reproduction/name).read_bytes()).hexdigest()==digest,name
        note("deterministic_regeneration",{"seed":manifest["seed"],"all_file_hashes_equal":True})
    report={"ok":True,"scope":"Synthetic fixture structure, cross-file consistency and optional deterministic regeneration only.","checks":checks,
            "not_tested":["Web/API correctness","30-client concurrent HTTP load","SSE delivery and reconnect","query latency","GNSS accuracy or repeatability","real phone or real factory deployment","unimplemented business state transitions"],
            "scenario_outcomes":"Expected acceptance criteria only; none are measured application results."}
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"ok":True,"checks":len(checks),"bulk_datasets":file_results,"scenario_fixtures":12,"report":str(report_path)},ensure_ascii=False))

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",type=Path)
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--reproduction",type=Path)
    args=parser.parse_args()
    validate(args.directory,args.report,args.reproduction)
