from fnos_media_import import create_app
from fnos_media_import.config import load_config


app = create_app()


if __name__ == "__main__":
    config = load_config()
    print(f"启动服务：{config.app_name}")
    print(f"访问地址：http://127.0.0.1:{config.port}")
    app.run(host=config.host, port=config.port, debug=config.debug)
