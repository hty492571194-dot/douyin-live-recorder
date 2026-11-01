"""测试包的全局隔离。

**跑测试会把构造数据写进正式业务日志**（`logs/monitor-YYYY-MM-DD.log`）。
历史上这已经反复造成困扰：日志里混进「主播A / 示例主播 / A」这类夹具条目；
最严重的一次是 2026-09-18 排查「服务反复重启」——把测试产生的
`[历史] 重新定位` / `[热点] A 开播时刻` 当成了真实的服务启动痕迹，
得出了完全错误的结论（实际 `launchctl` 只跑过 2 次）。

这里在导入任何 `test_*` 模块之前，把 `monitor.LOG_DIR` 指到临时目录，
从根上断掉这条污染路径。unittest 收集测试时会先导入本包，
所以这个赋值一定早于所有测试模块里的 `import monitor`。

要验证日志行为请**直接 patch `monitor.log`**（`tests/test_env_proxy.py`
与 `tests/test_ffmpeg_args.py` 已有先例），不要去读真实日志文件。
"""
import atexit
import shutil
import tempfile

try:
    import monitor  # noqa: E402

    _TEST_LOG_DIR = tempfile.mkdtemp(prefix="douyin-test-logs-")
    atexit.register(shutil.rmtree, _TEST_LOG_DIR, True)
    monitor.LOG_DIR = _TEST_LOG_DIR
except Exception:  # noqa: BLE001
    # 缺依赖等极端情况下不要阻断测试收集 —— 隔离失效只是噪音,不该让整套测试挂掉
    pass

# 崩溃事件流同样不能落到生产文件 `state/incidents.json`。
# 2026-10-30 就踩过:`tests/test_gift_cross_session.py` 直接调
# `monitor._restart_recording(...)`(不带 reason)、`test_record_health.py` 与
# `test_junk_segments.py` 直接调 `_give_up(...)`,而它们在字母序上排在
# `test_incidents` **之前** —— 那时 incidents 还指向默认路径,一次全量测试就把
# 「主播A 录制中断并重启」这类假事件写进了真实状态文件,界面告警条上真的出现了。
# 所以在这里先统一指到临时目录:任何测试模块里的 record 都只写 tmp。
try:
    import incidents  # noqa: E402

    _TEST_INCIDENT_DIR = tempfile.mkdtemp(prefix="douyin-test-incidents-")
    atexit.register(shutil.rmtree, _TEST_INCIDENT_DIR, True)
    incidents.configure(_TEST_INCIDENT_DIR + "/incidents.json")
except Exception:  # noqa: BLE001
    pass
