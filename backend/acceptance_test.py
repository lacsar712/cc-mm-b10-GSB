"""验收脚本：用 sqlite 临时库 + TestClient 跑通全部验收点。

可通过 DATABASE_URL 指定已有数据库；默认使用临时 sqlite 文件，无需数据库服务。
"""
import os
import tempfile

if "DATABASE_URL" not in os.environ:
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

with TestClient(app) as client:  # 触发 startup
    def login(username, password):
        r = client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    gasman = login("gasman", "gas123456")
    viewer = login("viewer", "view123456")
    passed = []

    def check(name, cond, detail=""):
        assert cond, f"FAIL: {name} {detail}"
        passed.append(name)
        print(f"PASS: {name} {detail}")

    # 0. 旧种子行没有三栏，仍显示，且三栏为 null
    r = client.get("/api/readings", headers=gasman)
    seed = r.json()
    check("旧种子行仍显示", len(seed) == 2, f"count={len(seed)}")
    check("旧种子三栏为空", all(row["wind_speed"] is None and row["tunnel_temp"] is None
                               and row["instrument_no"] is None for row in seed))

    # 1. 缺风速应停住：422，且不入库
    before = client.get("/api/readings", headers=gasman).json()
    r = client.post("/api/readings", headers=gasman, json={
        "site": "西大巷", "ch4_pct": 0.42,
        # 故意不给 wind_speed
        "tunnel_temp": "18℃", "instrument_no": "JFY-001",
    })
    check("缺风速返回422", r.status_code == 422, f"status={r.status_code} body={r.text}")
    locs = [e["loc"][-1] for e in r.json()["detail"]]
    check("错误指向wind_speed", "wind_speed" in locs, str(locs))
    after = client.get("/api/readings", headers=gasman).json()
    check("缺栏不入库", len(after) == len(before))

    # 缺巷温、缺仪器编号同样拒绝
    for payload, field in [
        ({"site": "西大巷", "ch4_pct": 0.42, "wind_speed": "2.1", "instrument_no": "JFY-001"}, "tunnel_temp"),
        ({"site": "西大巷", "ch4_pct": 0.42, "wind_speed": "2.1", "tunnel_temp": "18℃"}, "instrument_no"),
        ({"site": "西大巷", "ch4_pct": 0.42, "wind_speed": "  ", "tunnel_temp": "18℃",
          "instrument_no": "JFY-001"}, "wind_speed"),
    ]:
        r = client.post("/api/readings", headers=gasman, json=payload)
        check(f"缺/空白{field}也拒绝", r.status_code == 422 and
              field in [e["loc"][-1] for e in r.json()["detail"]], f"status={r.status_code}")

    # 2. 补齐后再报：201，三栏原文独立存储
    r = client.post("/api/readings", headers=gasman, json={
        "site": "西大巷", "ch4_pct": 0.42,
        "wind_speed": "2.4 m/s", "tunnel_temp": "18.5℃", "instrument_no": "JFY-001",
    })
    check("三栏齐全上报201", r.status_code == 201, r.text)
    new_id = r.json()["id"]
    check("三栏原文独立入库", r.json()["wind_speed"] == "2.4 m/s"
          and r.json()["tunnel_temp"] == "18.5℃" and r.json()["instrument_no"] == "JFY-001")

    # 3. 每次成功上报写三栏履历（首次录入 old 为 null）
    r = client.get(f"/api/readings/{new_id}/history", headers=gasman)
    hist = r.json()
    check("成功上报写三条履历", len(hist) == 3, f"len={len(hist)}")
    check("履历首次录入old为空", all(h["old"] is None for h in hist))
    by_field = {h["field"]: h for h in hist}
    check("履历存原文", by_field["tunnel_temp"]["new"] == "18.5℃"
          and by_field["wind_speed"]["new"] == "2.4 m/s"
          and by_field["instrument_no"]["new"] == "JFY-001")

    # 4. 用仪器编号片段过滤，只见到新行（旧种子 instrument 为空，被排除）
    r = client.get("/api/readings?instrument=JFY", headers=gasman)
    frag_rows = r.json()
    check("片段过滤只见新行", len(frag_rows) == 1 and frag_rows[0]["id"] == new_id,
          f"ids={[x['id'] for x in frag_rows]}")
    # 片段为部分匹配，大小写不敏感
    r = client.get("/api/readings?instrument=jfy", headers=gasman)
    check("片段部分/大小写不敏感", len(r.json()) == 1 and r.json()[0]["id"] == new_id)

    # 5. 改正该行巷温
    r = client.patch(f"/api/readings/{new_id}", headers=gasman,
                     json={"tunnel_temp": "19.2℃"})
    check("改正巷温成功", r.status_code == 200, r.text)
    r = client.get("/api/readings", headers=gasman)
    row = next(x for x in r.json() if x["id"] == new_id)
    check("当前巷温为新值", row["tunnel_temp"] == "19.2℃")

    # 6. 履历里仍能看到改正前的巷温（旧履历行原文不动，新增一行 old=18.5 new=19.2）
    r = client.get(f"/api/readings/{new_id}/history", headers=gasman)
    hist = r.json()
    temp_rows = [h for h in hist if h["field"] == "tunnel_temp"]
    check("巷温履历有两行", len(temp_rows) == 2, f"len={len(temp_rows)}")
    check("旧履历保留改正前原文",
          temp_rows[0]["old"] is None and temp_rows[0]["new"] == "18.5℃",
          str(temp_rows[0]))
    check("新履历记录旧→新",
          temp_rows[1]["old"] == "18.5℃" and temp_rows[1]["new"] == "19.2℃",
          str(temp_rows[1]))

    # 改正只影响被改的栏，其他栏履历不增加
    check("其他栏履历仍各一条",
          len([h for h in hist if h["field"] == "wind_speed"]) == 1
          and len([h for h in hist if h["field"] == "instrument_no"]) == 1)

    # 7. 旁观账号：能过滤、能看履历
    r = client.get("/api/readings?instrument=JFY", headers=viewer)
    check("旁观可过滤", r.status_code == 200 and len(r.json()) == 1)
    r = client.get(f"/api/readings/{new_id}/history", headers=viewer)
    check("旁观可看履历", r.status_code == 200 and len(r.json()) == 4)

    # 8. 旁观账号：不能上报、不能改正、不能新建方案
    r = client.post("/api/readings", headers=viewer, json={
        "site": "x", "ch4_pct": 0.1,
        "wind_speed": "1", "tunnel_temp": "1", "instrument_no": "X-1"})
    check("旁观上报403", r.status_code == 403, f"status={r.status_code}")
    r = client.patch(f"/api/readings/{new_id}", headers=viewer, json={"tunnel_temp": "20℃"})
    check("旁观改正403", r.status_code == 403)
    r = client.post("/api/filter-schemes", headers=viewer,
                    json={"name": "一仪", "instrument_fragment": "JFY"})
    check("旁观建方案403", r.status_code == 403, f"status={r.status_code}")

    # 9. 命名过滤方案：writer 保存 → 双方都可列出/一键套用；重复名 400
    r = client.post("/api/filter-schemes", headers=gasman,
                    json={"name": "甲仪JFY", "instrument_fragment": "JFY"})
    check("writer保存方案201", r.status_code == 201, r.text)
    sid = r.json()["id"]
    r = client.post("/api/filter-schemes", headers=gasman,
                    json={"name": "甲仪JFY", "instrument_fragment": "JFY"})
    check("方案重名400", r.status_code == 400)
    r = client.get("/api/filter-schemes", headers=viewer)
    check("旁观可列方案套用", r.status_code == 200
          and any(s["instrument_fragment"] == "JFY" for s in r.json()))
    # 旁观套用该方案过滤（模拟前端一键：取 fragment 调列表接口）
    frag = next(s["instrument_fragment"] for s in client.get(
        "/api/filter-schemes", headers=viewer).json() if s["id"] == sid)
    r = client.get(f"/api/readings?instrument={frag}", headers=viewer)
    check("旁观套用方案过滤生效", len(r.json()) == 1 and r.json()[0]["id"] == new_id)

    # 10. 未登录 401
    check("未登录读列表401", client.get("/api/readings").status_code == 401)

    # 11. 过滤不影响无片段时看到全部（含旧行）
    r = client.get("/api/readings", headers=gasman)
    check("无过滤看到全部3行", len(r.json()) == 3, f"len={len(r.json())}")

print(f"\n全部 {len(passed)} 项验收通过")
