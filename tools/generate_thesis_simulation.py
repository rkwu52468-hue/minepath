"""Deterministic synthetic input fixtures, not an application or load test."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import random

BASE = datetime(2026, 10, 28, 8, tzinfo=timezone(timedelta(hours=8)))
FIELDS = ["observation_id", "employee_id", "device_id", "session_id", "observed_at", "received_at", "x_m", "y_m", "uncertainty_m", "initial_team_id", "assigned_area_id", "area_version_id", "is_simulated"]

def ts(value):
    return value.isoformat(timespec="milliseconds")

def at(clock):
    hour, minute, second = map(int, clock.split(":"))
    return ts(BASE.replace(hour=hour, minute=minute, second=second))

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

def fixture(case_id, title, rules, actions, expected, context=None, assumptions=None):
    return {"scenario_id": case_id, "title": title, "is_simulated": True,
            "rule_refs": rules, "context": context or {}, "actions": actions,
            "expected": expected, "expectation_status": "acceptance_fixture_not_executed",
            "working_assumptions": assumptions or []}

def action(clock, actor, operation, **details):
    return {"at": at(clock), "actor_id": actor, "operation": operation, **details}

def obs(point_id, clock, received_clock=None, **details):
    return {"observation_id": point_id, "observed_at": at(clock),
            "received_at": at(received_clock or clock), "is_simulated": True, **details}

def scenarios():
    # Each case starts from an independent copy of the baseline organization.
    # Expectations are requirements for the future app, not outcomes of this tool.
    return [
        fixture("S01", "开始上报与正常结束", ["BR-007"], [
            action("08:00:00", "E001", "start_reporting", session_id="S01-session", area_id="A"),
            action("08:00:05", "E001", "submit_observation", observation=obs("S01-p1", "08:00:05", x_m=35, y_m=50)),
            action("08:30:00", "E001", "stop_reporting")],
            {"session_count": 1, "final_session_status": "ended", "observation_count": 1}),
        fixture("S02", "转场超时核查、延期与到达确认", ["BR-001", "BR-002", "BR-003", "BR-004", "BR-005", "BR-006"], [
            action("08:05:00", "L01", "issue_transfer", employee_id="E001", from_area="A", to_area="B", allowed_seconds=600),
            action("08:10:00", "E001", "start_transfer"),
            action("08:20:01", "system", "evaluate_transfer_deadline"),
            action("08:20:30", "L01", "ask_progress_and_reason"),
            action("08:21:00", "E001", "explain_delay", reason="模拟道路临时受阻"),
            action("08:21:30", "L01", "extend_transfer", new_deadline=at("08:25:00"), reason="已核实模拟绕行，延长5分钟"),
            action("08:22:00", "E001", "request_arrival_confirmation", claimed_arrival_at=at("08:22:00")),
            action("08:25:00", "L01", "confirm_arrival", confirmed_effective_at=at("08:22:00"))],
            {"original_deadline": at("08:20:00"), "extended_deadline": at("08:25:00"),
             "timeout_history_preserved": True, "automatic_termination": False,
             "area_effective_at": at("08:22:00"), "confirmation_operated_at": at("08:25:00"),
             "final_transfer_status": "completed"},
            assumptions=["本用例的延期以新的截止时刻表达；最终界面采用追加分钟数还是截止时间尚待设计。"]),
        fixture("S03", "中途调组与晚到轨迹的时间归属", ["BR-008", "BR-009"], [
            action("08:54:00", "L01", "issue_transfer", employee_id="E001", from_area="A", to_area="B", allowed_seconds=1200),
            action("08:55:00", "E001", "start_transfer", from_area="A", to_area="B"),
            action("09:00:00", "ADMIN", "change_team", employee_id="E001", from_team="T01", to_team="T02"),
            action("09:05:00", "E001", "upload_batch", observations=[obs("S03-old", "08:59:00", "09:05:00"), obs("S03-new", "09:01:00", "09:05:00")])],
            {"reporting_session_continues": True, "observation_team": {"S03-old": "T01", "S03-new": "T02"},
             "current_transfer_owner": "L02", "pre_transfer_history_visible_to_L02": False},
            context={"reporting_session_started_at": at("08:00:00"), "transfer_allowed_seconds": 1200},
            assumptions=["中途调组分段及未完成任务随员工交接是前述讨论采用的工作方案；数据模型和交接操作仍需落实。"]),
        fixture("S04", "接收班组负责人停用时仍可调组", ["BR-009", "BR-010"], [
            action("09:00:00", "ADMIN", "change_team", employee_id="E001", from_team="T01", to_team="T03"),
            action("09:02:00", "ADMIN", "appoint_temporary_leader", team_id="T03", leader_id="L02")],
            {"team_change_blocked": False, "assigned_temporary_leader": "L02", "appointment_audited": True},
            context={"leader_account_overrides": {"L03": "disabled"}},
            assumptions=["09:00至09:02接管空档的具体待办队列和通知机制尚未定稿，本用例不规定该细节。"]),
        fixture("S05", "既往事件不随调组自动移交", ["BR-008", "BR-011"], [
            action("08:50:00", "system", "create_zone_event", employee_id="E001", team_id="T01"),
            action("09:00:00", "ADMIN", "change_team", employee_id="E001", from_team="T01", to_team="T02"),
            action("09:10:00", "L01", "investigate_existing_event")],
            {"event_responsible_team": "T01", "employee_can_add_explanation": True,
             "new_team_gets_all_old_tracks": False}),
        fixture("S06", "求助主责连续且权限按单限定", ["BR-012"], [
            action("08:50:00", "E001", "submit_help", description="模拟设备故障求助"),
            action("08:51:00", "L01", "accept_help"),
            action("09:00:00", "ADMIN", "change_team", employee_id="E001", from_team="T01", to_team="T02"),
            action("09:10:00", "L02", "assist_help"),
            action("09:20:00", "E001", "report_help_unresolved")],
            {"help_owner": "L01", "new_team_may_assist": True, "help_remains_open": True,
             "owner_gets_all_new_team_tracks": False}),
        fixture("S07", "单采集端冲突与管理员换绑", ["BR-013"], [
            action("08:05:00", "E001", "attempt_second_collector", device_id="D031"),
            action("08:10:00", "ADMIN", "replace_collector", employee_id="E001", old_device="D001", new_device="D031", reason="模拟手机更换"),
            action("08:11:00", "E001", "upload_new_observation", device_id="D001", observed_at=at("08:11:00")),
            action("08:11:01", "E001", "upload_new_observation", device_id="D031", observed_at=at("08:11:01"))],
            {"second_collector_initially_rejected": True, "active_device_after_change": "D031",
             "old_device_post_change_observation_accepted": False, "existing_tracks_preserved": True},
            context={"reporting_session_started_at": at("08:00:00"), "active_device_id": "D001"}),
        fixture("S08", "终止时刻前后补传逐条接收", ["BR-014"], [
            action("08:10:00", "E001", "network_lost"),
            action("08:15:00", "ADMIN", "terminate_reporting_session"),
            action("08:20:00", "E001", "upload_batch", observations=[
                obs("S08-before", "08:14:59", "08:20:00"),
                obs("S08-equal", "08:15:00", "08:20:00"),
                obs("S08-after", "08:15:01", "08:20:00")])],
            {"accepted_ids": ["S08-before"], "rejected_ids": ["S08-equal", "S08-after"],
             "rejection_reason": "session_ended", "session_reopened": False},
            context={"reporting_session_started_at": at("08:00:00"), "device_authorization": "valid for all supplied observation times", "timestamps": "synthetic, assumed valid"}),
        fixture("S09", "乱序和重传不回退最新位置", ["BR-013", "BR-014"], [
            action("08:00:21", "E001", "upload_batch", observations=[
                obs("S09-new", "08:00:20", "08:00:21", x_m=40, y_m=50),
                obs("S09-old", "08:00:10", "08:00:21", x_m=30, y_m=50),
                obs("S09-old", "08:00:10", "08:00:21", x_m=30, y_m=50)])],
            {"unique_observation_count": 2, "latest_observation_id": "S09-new", "duplicate_insert_count": 0},
            context={"reporting_session_started_at": at("08:00:00")}),
        fixture("S10", "区域生效与迟到观测使用历史版本", ["BR-015"], [
            action("10:00:00", "ADMIN", "activate_area_version", area_id="A", version_id="A-v2"),
            action("10:05:00", "E001", "upload_batch", observations=[
                obs("S10-before", "09:59:59", "10:05:00", x_m=80, y_m=50),
                obs("S10-equal", "10:00:00", "10:05:00", x_m=80, y_m=50)])],
            {"area_version": {"S10-before": "A-v1", "S10-equal": "A-v2"},
             "geometric_reference_only": {"S10-before": "inside", "S10-equal": "outside"},
             "old_event_evidence_overwritten": False},
            assumptions=["几何参考结果不等于真实GPS越界算法验收；质量、连续观测及边界变更提示机制需单独实现测试。"]),
        fixture("S11", "再次进入形成独立事件", ["BR-016"], [
            action("10:00:00", "system", "confirmed_restricted_entry", employee_id="E001", area_id="R01"),
            action("10:01:00", "system", "confirmed_still_inside", employee_id="E001", area_id="R01"),
            action("10:03:00", "system", "confirmed_restricted_exit", employee_id="E001", area_id="R01"),
            action("10:06:00", "system", "confirmed_restricted_entry", employee_id="E001", area_id="R01")],
            {"episode_count": 2, "first_observation_status": "recovered", "first_investigation_status": "not_closed", "second_event_created": True},
            assumptions=["输入为已经达到进入或离开条件的判定，不用于测试尚未制定的连续观测阈值。"]),
        fixture("S12", "上报中断保留会话和真实缺口", ["BR-007"], [
            action("08:00:00", "E001", "start_reporting", session_id="S12-session", area_id="A"),
            action("08:00:05", "E001", "submit_observation", observation=obs("S12-p1", "08:00:05")),
            action("08:01:00", "E001", "network_lost"),
            action("08:10:00", "E001", "network_restored"),
            action("08:10:05", "E001", "submit_observation", observation=obs("S12-p2", "08:10:05"))],
            {"session_count": 1, "session_automatically_ended": False, "invented_gap_points": 0,
             "old_position_timestamp_visible": True},
            assumptions=["断网区间本用例没有实际观测输入；模拟器不插值生成补传点。中断判定阈值与覆盖时长计算口径尚未冻结。"])
    ]

def generate(output, seed, sizes):
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Output must be an empty directory; existing fixtures are never overwritten")
    teams = [{"team_id": f"T{i:02}", "name": f"模拟班组{i}", "leader_id": f"L{i:02}", "is_simulated": True} for i in range(1,4)]
    employees = [{"employee_id": f"E{i:03}", "login_name": f"sim_employee_{i:03}", "display_name": f"模拟员工{i:02}", "role": "employee", "initial_team_id": f"T{(i-1)//10+1:02}", "device_id": f"D{i:03}", "initial_area_id": "ABC"[(i-1)//10], "is_simulated": True} for i in range(1,31)]
    leaders = [{"account_id": f"L{i:02}", "login_name": f"sim_leader_{i:02}", "role": "team_leader", "team_id": f"T{i:02}", "is_simulated": True} for i in range(1,4)]
    admin = {"account_id": "ADMIN", "login_name": "sim_admin", "role": "admin", "is_simulated": True}
    write_json(output / "organization.json", {"is_simulated": True, "teams": teams, "employees": employees, "leaders": leaders, "administrator": admin,
               "note": "Logical account fixtures only. No real accounts, passwords or browser sessions are created."})
    write_csv(output / "employees.csv", list(employees[0]), employees)
    devices = [{"device_id": f"D{i:03}", "bound_employee_id": f"E{i:03}" if i<=30 else "", "is_simulated": True} for i in range(1,33)]
    write_csv(output / "devices.csv", list(devices[0]), devices)
    zero = BASE.replace(hour=0)
    area_rows=[]
    for index, area_id in enumerate("ABC"):
        x=index*200
        area_rows.append({"area_id": area_id, "version_id": area_id+"-v1", "kind": "work", "effective_from": ts(zero),
                          "effective_to": at("10:00:00") if area_id=="A" else None,
                          "polygon_xy_m": [[x,0],[x+100,0],[x+100,100],[x,100],[x,0]]})
    area_rows += [
        {"area_id":"A","version_id":"A-v2","kind":"work","effective_from":at("10:00:00"),"effective_to":None,"polygon_xy_m":[[0,0],[60,0],[60,100],[0,100],[0,0]]},
        {"area_id":"R01","version_id":"R01-v1","kind":"restricted","effective_from":ts(zero),"effective_to":None,"polygon_xy_m":[[125,30],[150,30],[150,70],[125,70],[125,30]]}]
    write_json(output / "areas.json", {"is_simulated":True,"coordinate_system":"synthetic_local_metre", "real_site":False,"area_versions":area_rows,
               "note":"Fictional local SVG-plane fixtures, not WGS84/GNSS coordinates and not RFC7946 GeoJSON. Leaflet integration needs an explicit coordinate adapter or calibration."})
    for count in sizes:
        rng=random.Random(seed)
        def rows():
            for index in range(count):
                employee=employees[index%30]
                tick=index//30
                observed=BASE+timedelta(seconds=tick*5)
                received=observed+timedelta(milliseconds=rng.randint(150,800))
                group=(index%30)//10
                phase=(index%30)*0.3
                x=group*200+35+10*math.sin(tick/15+phase)+rng.uniform(-0.5,0.5)
                y=50+15*math.cos(tick/20+phase)+rng.uniform(-0.5,0.5)
                uncertainty=round(rng.uniform(2,5),3)
                area=employee["initial_area_id"]
                yield dict(zip(FIELDS,[f"bulk-{index+1:06}", employee["employee_id"],employee["device_id"],f"sim-session-{index%30+1:03}",ts(observed),ts(received),round(x,3),round(y,3),uncertainty,employee["initial_team_id"],area,area+("-v2" if area=="A" and observed>=BASE.replace(hour=10) else "-v1"),True]))
        write_csv(output/f"positions_{count}.csv", FIELDS, rows())
    write_json(output / "scenarios.json", {"is_simulated":True,"isolation":"Each scenario resets the baseline organization; do not execute cases cumulatively.","scenarios":scenarios()})
    notes = """# 30人模拟输入与场景用例

本目录全部为虚构数据，不含真实员工、矿厂生产数据或手机GNSS采集结果。账号只是输入定义，未创建应用账号。当前也没有运行Web系统、30路HTTP并发或SSE负载测试。

- organization.json：3个班组、30名员工、3名负责人和1名管理员。
- employees.csv、devices.csv：30名员工及30个绑定采集端、2个备用逻辑采集端；逻辑ID不是手机硬件标识。
- areas.json：虚构平面坐标，单位米，包含A/B/C作业区、A区两个历史版本和受限区R01。不能直接当作经纬度或GeoJSON提交给地图服务。
- positions_10000.csv、positions_100000.csv：30路逻辑员工流交错存放。每个员工的模拟观测间隔为5秒，这是固定负载参数，不是手机采集保证或已确认的产品上报周期。
- 两个规模来自同一条确定性序列，1万条是10万条的前缀。分别测试时使用独立数据库或重置数据；不得直接相加后宣称有11万条独立观测。
- scenarios.json：12个独立场景、输入动作及未来系统的预期断言。不是实际运行结果，也没有调用应用API。每个场景从基线状态重置；不把各场景串行累积。
- 工作方案和未定细节在各场景的working_assumptions中说明。连续观测、边界不确定度、会话覆盖时长等参数尚未冻结，不能由本数据推导算法已正确。
- manifest.json：随机种子、固定模拟时间、规模、文件哈希和证据边界。

原始观测时间和模拟接收时间分别保存。常规轨迹不插入人为缺口；断网、重复、乱序及终止边界由独立场景明确构造。真实手机数据必须单独保存在evidence/raw，不得改标签并混用为真实证据。
"""
    (output/"README.md").write_text(notes,encoding="utf-8")
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}
    manifest={"schema_version":1,"is_simulated":True,"seed":seed,"simulation_start":ts(BASE),"timezone":"Asia/Shanghai","team_count":3,"employee_count":30,"leader_count":3,"admin_count":1,"logical_device_count":32,"position_counts":sizes,"logical_sampling_interval_seconds":5,"scenario_count":len(scenarios()),"source_rule_ids":[f"BR-{i:03}" for i in range(1,18)],"files_sha256":hashes,"evidence_scope":"Synthetic input generation and structural validation only; no application execution, load capacity, positioning accuracy or real-site validation."}
    write_json(output/"manifest.json",manifest)
    print(json.dumps({"output":str(output),"employee_count":30,"position_counts":sizes,"scenario_count":12,"is_simulated":True},ensure_ascii=False))

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--seed",type=int,default=20261028)
    parser.add_argument("--sizes",type=int,nargs="+",default=[10000,100000])
    args=parser.parse_args()
    if any(n<=0 for n in args.sizes) or len(set(args.sizes))!=len(args.sizes):
        parser.error("Sizes must be unique positive integers")
    generate(args.output,args.seed,args.sizes)
