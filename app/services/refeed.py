import sqlite3

from app.db import connect
from app.services.objects import ServiceError

REFEED_MODE = "复制保留"


def list_refeed_runs(db_path: str | None = None) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM refeed_runs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def refeed_object(
    source_batch_id: int,
    target_batch_id: int,
    object_id: int,
    db_path: str | None = None,
) -> dict:
    """把已闭合对象复制到收集中批继续补采（复制保留路径）。

    单事务一次做完：BEGIN IMMEDIATE 立即取库写锁串行化并发，
    refeed_locks 以源对象为主键做持久排他——同一源对象只许回灌一次，
    两侧检修并发抢同一对象时一成一败，不会双成功盖写。
    旧批闭合标记与旧对象只读不动，副本带溯源列写入新批。
    """
    conn = connect(db_path)
    # 关闭模块级隐式事务，手工控制 BEGIN IMMEDIATE
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        source_batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (source_batch_id,)
        ).fetchone()
        if not source_batch:
            raise ServiceError("源批次不存在", 404)
        target_batch = conn.execute(
            "SELECT * FROM landing_batches WHERE id=?", (target_batch_id,)
        ).fetchone()
        if not target_batch:
            raise ServiceError("目标批次不存在", 404)
        if source_batch_id == target_batch_id:
            raise ServiceError("源批次与目标批次不能相同，混批即停", 409)
        if source_batch["status"] != "已闭合":
            raise ServiceError("仅已闭合批次的对象可回灌", 409)
        if target_batch["status"] != "收集中":
            raise ServiceError("仅收集中批次可接收回灌对象", 409)

        obj = conn.execute(
            "SELECT * FROM landing_objects WHERE id=?", (object_id,)
        ).fetchone()
        if not obj:
            raise ServiceError("源对象不存在", 404)
        # 混批即停：对象必须确实归属源批，且为已闭合状态
        if obj["batch_id"] != source_batch_id:
            raise ServiceError("对象不属于源批次，混批即停", 409)
        if obj["status"] != "已闭合":
            raise ServiceError("仅已闭合对象可回灌", 409)

        dup_target = conn.execute(
            "SELECT id FROM landing_objects WHERE batch_id=? AND object_key=?",
            (target_batch_id, obj["object_key"]),
        ).fetchone()
        if dup_target:
            raise ServiceError("目标批次已存在同键对象，混批即停", 409)

        cur = conn.execute(
            "INSERT INTO refeed_runs("
            "source_batch_id, target_batch_id, source_object_id, mode, outcome, detail) "
            "VALUES (?,?,?,?,'失败','')",
            (source_batch_id, target_batch_id, object_id, REFEED_MODE),
        )
        run_id = cur.lastrowid

        # 持久排他锁：同对象二次回灌（含并发抢锁）在此撞主键失败
        try:
            conn.execute(
                "INSERT INTO refeed_locks(source_object_id, refeed_run_id) VALUES (?,?)",
                (object_id, run_id),
            )
        except sqlite3.IntegrityError:
            raise ServiceError("该对象已回灌，排他锁拒绝重复回灌", 409)

        new_cur = conn.execute(
            "INSERT INTO landing_objects("
            "batch_id, object_key, content_hash, byte_size, status, "
            "source_object_id, source_batch_id) "
            "VALUES (?,?,?,?,'已登记',?,?)",
            (
                target_batch_id,
                obj["object_key"],
                obj["content_hash"],
                obj["byte_size"],
                object_id,
                source_batch_id,
            ),
        )
        new_object_id = new_cur.lastrowid
        conn.execute(
            "UPDATE refeed_runs SET new_object_id=?, outcome='已复制', detail=? WHERE id=?",
            (
                new_object_id,
                f"复制保留：源对象 #{object_id} 原样留旧批只读，副本 #{new_object_id} "
                f"入目标批 #{target_batch_id} 继续补采",
                run_id,
            ),
        )
        conn.execute("COMMIT")
    except ServiceError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ServiceError(f"回灌入库冲突，已停：{exc}", 409)
    except sqlite3.OperationalError as exc:
        conn.execute("ROLLBACK")
        raise ServiceError(f"回灌抢锁失败，请重试：{exc}", 409)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    conn = connect(db_path)
    try:
        run = conn.execute(
            "SELECT * FROM refeed_runs WHERE id=?", (run_id,)
        ).fetchone()
        new_obj = conn.execute(
            "SELECT * FROM landing_objects WHERE id=?", (new_object_id,)
        ).fetchone()
        return {"run": dict(run), "object": dict(new_obj), "mode": REFEED_MODE}
    finally:
        conn.close()
