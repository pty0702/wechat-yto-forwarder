# 微信 ⇄ 圆通客户管家消息转发

Python 桌面工具：按运单号和关键词处理微信群文本，将请求转到客户管家，再将机器人的相关回复搬回微信。

## 当前可用内容

- 中文配置窗口，启动、停止、实时日志和独立演示。
- YT 运单筛选、屏蔽词、@客服分流、完整机器人昵称匹配、30 秒静默超时。
- 消息事件去重、发送失败缓存、发送结果不确定时停止，避免盲目重发。
- Windows UI Automation 适配器、只读控件诊断、启动历史基线、目标群和发送回显检查。

**真实双向收发尚未完成本机验收。** 2026-10-23 检查发现：本机微信 `4.1.15.56` 只暴露窗口和窗格，没有消息列表或输入框；客户管家 `2.0.0.2` 可以读取聊天控件。真实运行需要客户端暴露完整控件，并根据诊断填写适配配置。仓库中的 `uia.example.json` 只是配置格式示例，不能当作微信 4.1 的现成配置。

## 启动

需要 Windows、Python 3.9+（含 Tkinter）和 PowerShell 7。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\python版\requirements.txt
.\.venv\Scripts\python.exe -X utf8 .\python版\gui.py
```

已有虚拟环境时，也可使用项目内的 `启动.ps1`。

界面默认选择演示模式，点击“开始运行”可查看约 12 秒的内置案例。演示只在内存处理样例，不需要真实群，也不会保存样例配置。

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s .\python版\tests -v
.\.venv\Scripts\python.exe -X utf8 .\python版\wechat_forwarder.py
```

详细业务规则、适配配置格式、已知限制与诊断说明见 [Python 版说明](python版/README.md)。

本地 `config.json`、`profiles/local*.json`、`diagnostics/`、虚拟环境和缓存不提交到 Git。
