# Flow2API Token Updater v3.0

轻量版 Token 自动更新工具，通过 Cookie 导入方式管理 Google Labs 登录状态。

## 特性

- 🪶 轻量化：去除 VNC，镜像体积大幅减小
- 🍪 Cookie 导入：在本地浏览器登录后导出 Cookie
- 🔄 自动刷新：定时使用 Cookie 刷新 Token
- 👥 多 Profile：支持管理多个账号
- 🌐 代理支持：每个 Profile 可配置独立代理

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/genz27/flow2api_tupdater.git
cd flow2api_tupdater

# 配置环境变量
cp .env.example .env
# 编辑 .env 设置 ADMIN_PASSWORD 等

# 启动
docker-compose up -d
```

访问 http://localhost:8002 进入管理界面。

## 使用流程

1. 创建 Profile
2. 在本地浏览器登录 https://labs.google
3. 使用浏览器插件（如 EditThisCookie）导出 Cookie
4. 在管理界面导入 Cookie
5. 配置 Flow2API 连接信息
6. 开始自动同步

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| ADMIN_PASSWORD | 管理界面密码 | admin123 |
| API_KEY | 外部 API 密钥 | - |
| FLOW2API_URL | Flow2API 地址 | http://host.docker.internal:8000 |
| CONNECTION_TOKEN | Flow2API 连接 Token | - |
| REFRESH_INTERVAL | 刷新间隔(分钟) | 60 |

## API

### 外部 API (需要 X-API-Key)

- `GET /v1/profiles` - 列出所有 Profile
- `GET /v1/profiles/{id}/token` - 获取 Token
- `POST /v1/profiles/{id}/sync` - 同步到 Flow2API

## 从 v2.0 升级

v3.0 移除了 VNC 功能，改用 Cookie 导入方式：

1. 备份 `data/` 目录
2. 拉取新版本
3. 重新构建镜像
4. 为每个 Profile 重新导入 Cookie

## License

MIT
