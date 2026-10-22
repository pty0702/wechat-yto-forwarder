"""真实客户端的单次检查与固定文本联调，不启动转发循环，也不自动重试。

记录只含状态、群名和运行摘要，不保存消息快照或控件树。调用方必须先取得
用户对目标端与测试发送的确认；本模块本身不显示弹窗。
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from wechat_forwarder import SendUncertainError, validate_config
from windows_adapter import create_live_adapters


TEST_MESSAGE = "转发工具联调测试，请忽略。"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _output_directory(output_dir):
    output = Path(output_dir).resolve()
    if not output.is_relative_to(PROJECT_ROOT):
        raise ValueError("联调记录目录必须位于当前项目内")
    if "diagnostics" not in output.relative_to(PROJECT_ROOT).parts:
        raise ValueError("联调记录必须放在项目内已由 Git 忽略的 diagnostics 目录下")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _write_report(path, record):
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_live_check(cfg, output_dir, stop_event, log=print, *, side=None, mention=False) -> Path:
    """检查双端准备，或仅向指定端发送一次固定文本；退出恢复后落盘结果。

    停止请求仅在准备前、准备后生效。发送开始后仍等待确认和恢复，不把已发送
    误报为取消。所有实际操作异常先记录再原样抛出；目录和参数错误在操作前拒绝。
    """
    if side not in (None, "wx", "kf"):
        raise ValueError("联调发送端只能是 wx、kf 或 None")
    if type(mention) is not bool or (mention and side != "wx"):
        raise ValueError("mention 必须为布尔值，且只有微信端允许测试真实 @")
    if mention and (not isinstance(cfg, dict) or not isinstance(cfg.get("service_staff"), str)
                    or not cfg["service_staff"].strip()):
        raise ValueError("测试真实 @ 前必须填写客服完整名称")
    output = _output_directory(output_dir)
    started = datetime.now(timezone.utc)
    path = output / f"live-check-{started.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}.json"
    groups = {key: cfg.get(key + "_group", "") for key in ("wx", "kf")} if isinstance(cfg, dict) else {}
    groups = {key: value if isinstance(value, str) else "" for key, value in groups.items()}
    record = {
        "time": started.isoformat(), "kind": "preflight" if side is None else "test_send",
        "side": side, "mention": mention, "group": groups if side is None else groups.get(side, ""),
        "status": "running", "errors": [], "logs": [], "phase": "validate",
        "send_started": False, "send_confirmed": False,
    }
    observed_errors = []
    uncertain = False
    active_error = None

    def emit(message):
        record["logs"].append(str(message))
        try:
            log(str(message))
        except Exception:
            # 日志显示失效不应使已发送消息再次发送，也不能遮蔽原操作异常。
            record["logs"].append("日志回调异常，完整结果仍写入本地记录")

    def adapter_log(message):
        # 适配器将来可能记录控件正文，故其原始日志仅显示，不随联调记录落盘。
        record["logs"].append("收到适配器状态日志，原文未写入联调记录")
        try:
            log(str(message))
        except Exception:
            record["logs"].append("适配器日志回调异常")

    def remember_error(error):
        nonlocal uncertain
        if any(error is previous for previous in observed_errors):
            return
        observed_errors.append(error)
        uncertain = uncertain or isinstance(error, SendUncertainError)
        record["errors"].append({
            "type": type(error).__name__, "message": str(error), "phase": record["phase"],
        })

    # 首次落盘在任何客户端操作前完成，目录不可写时不会触发发送。
    try:
        _write_report(path, record)
    except Exception as exc:
        try:
            log(f"[联调] 记录无法创建，未操作客户端：{path}；{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise
    try:
        if stop_event.is_set():
            record.update(status="cancelled", phase="cancelled")
            emit("[联调] 已在准备窗口前取消")
        else:
            live_cfg = validate_config(cfg, live=True)
            if mention and not live_cfg["service_staff"]:
                raise ValueError("测试真实 @ 前必须填写客服完整名称")
            record["group"] = ({key: live_cfg[key + "_group"] for key in ("wx", "kf")}
                               if side is None else live_cfg[side + "_group"])
            record["phase"] = "prepare"
            emit("[联调] 正在核对双端窗口并建立历史快照")
            with create_live_adapters(live_cfg, adapter_log) as adapters:
                try:
                    if stop_event.is_set():
                        record["status"] = "cancelled"
                        emit("[联调] 已在窗口准备后取消，未执行测试发送")
                    elif side is not None:
                        record["phase"] = "send"
                        sink = adapters[1 if side == "wx" else 3]
                        emit("[联调] 正在执行一次固定文本测试发送" + ("（真实 @）" if mention else ""))
                        record["send_started"] = True
                        if mention:
                            sink.send_mention(live_cfg["service_staff"], TEST_MESSAGE)
                        else:
                            sink.send(TEST_MESSAGE)
                        record["send_confirmed"] = True
                    else:
                        emit("[联调] 双端准备检查已完成，正在恢复本次兼容状态")
                except Exception as exc:
                    remember_error(exc)
                    raise
                finally:
                    record["phase"] = "restore"
            # 必须让上下文退出完成，不能把恢复失败记录为成功。
            if record["status"] == "cancelled":
                record["phase"] = "cancelled"
            else:
                record.update(status="success", phase="complete")
                emit("[联调] 检查与恢复已完成" if side is None else "[联调] 单次测试发送与恢复已完成")
    except Exception as exc:
        active_error = exc
        remember_error(exc)
        record["status"] = "uncertain" if uncertain else "failed"
        record["phase"] = record["errors"][-1]["phase"]
        emit("[联调] 发送结果不确定，未重试；请人工核对" if uncertain else "[联调] 本次检查失败，未重试")
        raise
    finally:
        try:
            _write_report(path, record)
        except Exception as report_error:
            try:
                log(f"[联调] 记录写入失败：{path}；{type(report_error).__name__}: {report_error}")
            except Exception:
                pass
            # 若已有操作异常，保留其原对象和调用链，不能被磁盘异常替代。
            if active_error is None:
                if record["send_started"]:
                    state = "已确认完成" if record["send_confirmed"] else "已开始"
                    raise SendUncertainError(
                        f"测试发送{state}，但本地报告写入失败；请人工核对，勿重试：{path}"
                    ) from report_error
                raise
        else:
            try:
                log(f"[联调] 记录已保存：{path}")
            except Exception:
                pass
    return path
