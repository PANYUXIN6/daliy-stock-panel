"""历史示例 4 — SSE 票据与事件流；当前应用已停用此入口。

旧版流程为 Bearer Token 换取一次性票据，再订阅继承 scope 的事件流。
适用范围见同目录 README.md。
"""
import itertools

from common import call, sse

# 1. 换票 (任意 scope 的 Token 都可; 流里只推票据 scope 覆盖的事件)
ticket = call("POST", "/api/events/ticket")["ticket"]
print("票据已签发 (60s 内一次性)")

# 2. 订阅: hello 帧之后, 面板触发任意监控告警即可看到 alert 事件
for event in itertools.islice(sse(f"/api/events?ticket={ticket}"), 5):
    if event.get("event") == "hello":
        print("hello: 票据 scope =", event.get("data", ""))
        continue
    print(f"[{event.get('event')}] {event.get('data', '')[:120]}")
print("(收到 5 帧后退出; 生产环境保持连接持续接收)")
