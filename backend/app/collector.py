"""唯一采集/任务执行服务入口（仅绑定本机或私有容器网络）。"""
from app.main import create_app

app = create_app("collector")
