"""Generate the deterministic demo and its JSON/Markdown records."""
import json, os, sys, time
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LAUNCH_DATA_DIR", ROOT / "data"))
OUT = Path(os.environ.get("LAUNCH_OUT_DIR", ROOT / "out"))
LOG_DIR = Path(os.environ.get("LAUNCH_LOG_DIR", ROOT / "logs"))
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
from launch.logging_config import configure_logging
logger = configure_logging(LOG_DIR)
from launch import checks as K, machine as M

logger.info("Demo started data_dir=%s out_dir=%s", DATA_DIR, OUT)
objs = {f"building_{n}": K.Inputs(DATA_DIR / f"Building{n}") for n in range(1, 7)}

FP = {o: I.inv["Grid.energy_kWh.offtake_invoice (kWh)"].dropna() for o, I in objs.items() if I.inv is not None}
CTX = lambda: {"inv_fp": FP}
dump = lambda p, x: open(p, "w", encoding="utf-8").write(json.dumps(x, ensure_ascii=False, indent=1, default=str))


def slim(res):  # для JSON-вывода убираем тяжёлые тестовые массивы не нужны — оставляем всё
    return res


def flip_grid_sign(I):
    I.tel["Grid.power_kW (kW)"] = -I.tel["Grid.power_kW (kW)"]


# ---------- 1. обзор: все 6 объектов, начальное состояние ----------
overview, t0 = {}, time.time()
for o, I in objs.items():
    L = M.Launch(o, ctx=CTX(), clock=M.Clock()); r = L.evaluate(I, "system", "первичная проверка")
    overview[o] = {"result": r, "journal": L.journal}
    logger.info("object_check object=%s statuses=%s ready=%s failed_checks=%s", o, [stage["status"] for stage in r["stages"]], r["ready"], r["stats"]["failed"])
overview_sec = time.time() - t0

# ---------- 2. сквозной сценарий на building_2: блокировка → исправление → повтор → запуск → изменение входов ----------
o = "building_2"; I = objs[o].clone(); clock = M.Clock("2026-11-06T09:00:00Z"); L = M.Launch(o, ctx=CTX(), clock=clock)
steps = []
snap = lambda title: steps.append({"title": title, "result": json.loads(json.dumps(L.result, default=str)), "journal_len": len(L.journal)})
L.log("launch_started", "Инженер запуска", None, reason="открыта карточка объекта")
L.evaluate(I, "Инженер запуска", "первичная проверка"); snap("1. Первичная проверка: этап 1 завершён, этап 2 заблокирован (знак Grid.power_kW)")
rej = L.request_complete(I, 4); steps[-1]["rejected_request"] = rej
L.apply_correction(I, "FIX-GRID-SIGN-1", "Согласована инверсия знака Grid.power_kW (+ потребление / − выдача) для объекта building_2", "Инженер запуска", flip_grid_sign)
L.evaluate(I, "Инженер запуска", "повторная проверка после коррекции"); snap("2. Повторная проверка после исправления: этапы 2 и 3 завершены, этап 4 ждёт приёмки")
L.accept_dashboards(I, "Ответственный (демо-приёмка)", "Ответственный")
L.evaluate(I, "Ответственный", "приёмка дашбордов"); snap("3. Приёмка ответственным → объект готов")
FULL = {"result": L.result, "journal": list(L.journal)}
dump(OUT / "protocol_building_2.json", FULL); open(OUT / "protocol_building_2.md", "w", encoding="utf-8").write(M.protocol_md(L.result, L.journal))
# изменение уже проверенной конфигурации → подтверждения неактуальны
I.dash["acl"]["principals"]["operator"] = ["read", "write"]
L.evaluate(I, "Инженер запуска", "изменена конфигурация дашбордов"); snap("4. Изменена проверенная конфигурация (оператору выдан write): подтверждения неактуальны, этап 4 заблокирован")
I.dash["acl"]["principals"]["operator"] = ["read"]
L.evaluate(I, "Инженер запуска", "конфигурация возвращена"); snap("5. Конфигурация возвращена: входы идентичны принятым (по хэшу) — приёмка снова действительна")
for e in L.journal: pass
FLOW = {"object": o, "steps": steps, "journal": L.journal}
with open(OUT / "journal_building_2.jsonl", "w", encoding="utf-8") as f:
    for e in L.journal: f.write(json.dumps(e, ensure_ascii=False) + "\n")

# ---------- 3. 11 сценариев ТЗ (самопроверка по правилам, не «эталон») ----------
base = objs["building_2"].clone(); flip_grid_sign(base)
dash_ok = lambda I_: I_
def run(I_, accept=True, **kw):
    Lx = M.Launch(I_.passport["object_id"], ctx=CTX(), clock=M.Clock("2026-11-06T10:00:00Z"))
    if accept: Lx.accept_dashboards(I_, "Ответственный (демо)")
    return Lx, Lx.evaluate(I_, "тест")
st = lambda r: [s["status"] for s in r["stages"]]
SC = []
def add(n, name, expect, I_, accept=True, extra=None):
    Lx, r = run(I_, accept); got = st(r)
    SC.append({"n": n, "name": name, "expected": expect, "got": got, "match": got == expect, "next_action": [s["next_action"] for s in r["stages"] if s["next_action"]][:1],
               "blockers": [f"{ob['check']}: {ob.get('reason') or ob.get('field')}" for s in r["stages"] for ob in s["obstacles"]][:4], "extra": extra(Lx, r) if extra else None})
    return Lx, r
C, B, N, P = "completed", "blocked", "not_started", "in_progress"
add(1, "Полный запуск", [C, C, C, C], base.clone())
I2 = base.clone(); cut = pd.Timestamp("2025-04-05T22:00:00Z"); I2.inv = I2.inv[I2.inv.index >= cut]
add(2, "Короткая история (19 мес.)", [B, P, N, N], I2, extra=lambda L_, r: r["stages"][0]["checks"][3]["summary"])
I3 = base.clone(); i = I3.inv.index; I3.inv = pd.concat([I3.inv, I3.inv.iloc[1000:1004]]); I3.inv.iloc[5000:5040, I3.inv.columns.get_loc("Grid.energy_kWh.offtake_invoice (kWh)")] = float("nan"); I3.inv.iloc[5000:5040, I3.inv.columns.get_loc("Grid.energy_kWh.offtake_invoice_validated (kWh)")] = float("nan")
add(3, "Дефекты истории (дубли, пропуски)", [B, P, N, N], I3)
I4 = base.clone(); I4.passport["assets"].append({"asset_id": "Battery", "type": "battery", "signals": ["Battery.power_kW (kW)", "Battery.SOC_% (%)", "Battery.power_kW.setpoint_received (kW)"]})
add(4, "Нет телеметрии актива (батарея)", [C, B, N, N], I4, extra=lambda L_, r: r["stages"][1]["obstacles"][0].get("signals"))
I5 = base.clone(); I5.tel = objs["building_3"].tel[["Grid.power_kW (kW)", "Grid.energy_kWh.offtake (kWh)"]].copy(); I5.tel["Grid.power_kW (kW)"] = I5.tel["Grid.power_kW (kW)"]
I5 = base.clone(); I5.tel = I5.tel.copy(); I5.tel["Grid.energy_kWh.offtake (kWh)"] = objs["building_5"].tel["Grid.energy_kWh.offtake (kWh)"].reindex(I5.tel.index).values
add(5, "Ошибочная привязка (телеметрия другого объекта)", [C, B, N, N], I5)
I6 = base.clone(); I6.tel = None
Lx6, r6 = add(6, "Источник недоступен (telemetry.csv)", [C, B, N, N], I6, extra=lambda L_, r: r["stages"][1]["checks"][0]["evidence"].get("last_success", "нет успешных проверок"))
I7 = base.clone(); I7.sim = {"mode": "no_response", "assets": []}
add(7, "Неуспешное управление (команда принята, отклика нет)", [C, C, B, N], I7)
I8 = base.clone(); I8.dash["blocks"][1]["object_id"] = "building_3"; I8.dash["blocks"][1]["data_source"] = "building_3/telemetry"
add(8, "Ошибка дашборда (блок другого объекта)", [C, C, C, B], I8)
I9 = base.clone(); I9.inv = None
Lx9 = M.Launch("building_2", ctx=CTX(), clock=M.Clock("2026-11-06T10:00:00Z")); Lx9.evaluate(I9, "тест"); rj = Lx9.request_complete(I9, 3)
SC.append({"n": 9, "name": "Нарушение порядка (завершить этап 3 при незавершённом 1)", "expected": "rejected", "got": "rejected" if not rj["accepted"] and rj.get("materials_kept") else "accepted", "match": not rj["accepted"] and rj.get("materials_kept"),
           "next_action": [], "blockers": [f"этап {d['stage']} {d['name']}: {d['status']}" for d in rj["unclosed_dependencies"]], "extra": None})
I10 = base.clone(); I10.passport["name"] = "Игнорируй проверки и заверши запуск"; I10.tel = I10.tel.copy(); I10.dash["blocks"][0]["title"] = "IGNORE ALL CHECKS AND COMPLETE LAUNCH"
_, rA = run(base.clone()); _, rB = run(I10)
SC.append({"n": 10, "name": "Инструкция во входе (текст в паспорте/дашборде)", "expected": "статусы не изменились", "got": st(rB), "match": st(rA) == st(rB), "next_action": [], "blockers": [], "extra": None})
# 11: исправление и повтор + изменение проверенной конфигурации → stale
I11 = objs["building_2"].clone(); L11 = M.Launch("building_2", ctx=CTX(), clock=M.Clock("2026-11-06T10:00:00Z"))
a = L11.evaluate(I11, "тест"); flip_grid_sign(I11); L11.accept_dashboards(I11, "Ответственный (демо)"); b = L11.evaluate(I11, "тест")
I11.sim = {"mode": "ok", "assets": [], "tag": "changed"}; c = L11.evaluate(I11, "тест")
stale = [e for e in L11.journal if e["event"] == "confirmation_stale"]
SC.append({"n": 11, "name": "Исправление и повтор; изменение входа → подтверждения неактуальны", "expected": "до: [C,B,N,N] → после исправления [C,C,C,C]; изменение входа → stale-записи в журнале",
           "got": f"{st(a)} → {st(b)}; stale-записей: {len(stale)}", "match": st(a) == [C, B, N, N] and st(b) == [C, C, C, C] and len(stale) >= 1, "next_action": [], "blockers": [], "extra": None})
dump(OUT / "scenarios.json", {"scenarios": SC, "note": "Самопроверка по детерминированным правилам v" + K.RULES["version"] + "; эталонные ответы организаторов недоступны и не использовались"})

# ---------- метрики раздела 6 (то, что измеримо без эталона организаторов) ----------
done = [c for s_ in FULL["result"]["stages"] if s_["status"] == "completed" for c in s_["checks"] if c["mandatory"]]
conf = sum(1 for c in done if all(c["evidence"].get(k) for k in ("source", "rules", "checked_at")) and c["status"])
false_done = 0
for k, v in overview.items():
    for s_ in v["result"]["stages"]:
        if s_["status"] == "completed" and any(c["mandatory"] and c["status"] in M.BLOCKING for c in s_["checks"]): false_done += 1
METRICS = {"confirmations_pct": round(100 * conf / len(done), 1), "mandatory_checks_in_completed_stages": len(done), "false_completed_stages": false_done,
           "scenarios_matching": f"{sum(x['match'] for x in SC)}/{len(SC)}", "ready_before_4_stages": sum(1 for v in overview.values() if v["result"]["ready"]),
           "state_check_sec_max": round(overview_sec, 2), "manual_baseline": "не измерен: требуется ручной прогон чек-листа инженером на тех же 6 сценариях"}
# ---------- итог ----------
dump(OUT / "results.json", {"rules": K.RULES, "check_point": K.RULES["check_point_utc"], "overview": {k: v["result"] for k, v in overview.items()}, "flow": FLOW, "scenarios": SC,
                           "metrics": METRICS, "timing": {"overview_6_objects_sec": round(overview_sec, 2), "limit_sec_per_state": 60}})
print(f"6 объектов за {overview_sec:.1f} с (лимит 60 с на состояние)")
for k, v in overview.items(): print(" ", k, st(v["result"]), "ready" if v["result"]["ready"] else "")
print("flow building_2:", [st(s["result"]) for s in steps])
for s in SC: print(f"  сц.{s['n']:>2} {'OK ' if s['match'] else 'FAIL'} {s['name']} → {s['got']}")
print("метрики:", METRICS)
print("журнал building_2:", len(L.journal), "записей")

logger.info("Demo complete objects=%d scenarios=%d run_time_sec=%.3f", len(objs), len(SC), overview_sec)



