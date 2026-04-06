# Anything AI Proxy

一个 Anything AI 的反向代理服务，提供 Anthropic Messages API 兼容接口，支持多账号管理和负载均衡。

## 功能特性

- **API 兼容**: 完全兼容 Anthropic Messages API 格式
- **多账号管理**: 支持添加多个 Anything AI 账号，自动负载均衡
- **批量导入 Outlook**: 从 Outlook 邮箱批量导入账号凭证
- **批量自动登录**: 通过 Magic Link 自动登录并获取 Token
- **Web 管理后台**: 可视化管理账号、查看使用统计
- **流式响应**: 支持 SSE 流式输出
- **Token 自动刷新**: 自动维护账号登录状态
- **数据持久化**: 支持 SQLite 和 PostgreSQL
- **Redis 缓存**: 可选 Redis 支持，适合多进程部署

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填写配置：

```bash
cp .env.example .env
```

必填项：
- `ACCESS_TOKEN`: Anything AI 访问令牌
- `REFRESH_TOKEN`: Anything AI 刷新令牌
- `PROJECT_GROUP_ID`: 项目组 ID

可选项：
- `API_KEY`: 保护代理接口的 API 密钥
- `DATABASE_URL`: PostgreSQL 连接字符串（默认使用 SQLite）
- `REDIS_URL`: Redis 连接字符串
- `PROXY_URL`: HTTP 代理地址

### 3. 获取 Anything AI 凭证

1. 登录 [Anything.com](https://www.anything.com)
2. 打开浏览器开发者工具（F12）
3. 切换到 Network 标签
4. 刷新页面，找到任意 GraphQL 请求
5. 在请求头中找到 `Authorization: Bearer <token>`
6. 复制 token 作为 `ACCESS_TOKEN`
7. 在 Application/Storage 中找到 `refresh_token` 和 `project_group_id`

### 4. 启动服务

```bash
python main.py
```

服务将在 `http://localhost:8000` 启动。

管理后台: `http://localhost:8000/admin/`

## API 使用

### 兼容 Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic(
    api_key="your-api-key",  # 如果设置了 API_KEY
    base_url="http://localhost:8000/v1"
)

response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### 支持的模型

- `claude-opus-4-6`
- `claude-sonnet-4-6`
- `claude-sonnet-4-5`
- `claude-haiku-4-5`
- `gpt-5.4` (映射到 ChatGPT 集成)

## 管理后台

访问 `http://localhost:8000/admin/` 进入管理后台：

- 添加/删除账号
- 查看账号状态和使用统计
- 监控请求日志
- 配置负载均衡策略
- **批量导入 Outlook 账号**
- **批量自动登录**

默认密码: `admin` (可通过 `ADMIN_PASSWORD` 环境变量修改)

### 批量导入 Outlook 账号

在管理后台可以批量导入 Outlook 账号凭证，格式为每行一个账号：

```
email----password----client_id----ms_refresh_token
```

导入后，系统会自动通过 IMAP + OAuth2 连接 Outlook 邮箱，用于接收 Anything AI 的 Magic Login Link 邮件。

### 批量自动登录

导入 Outlook 账号后，可以批量触发自动登录流程：

1. 系统向 Anything AI 发送登录请求，触发 Magic Link 邮件
2. 自动从 Outlook 邮箱读取 Magic Link
3. 自动打开链接并提取 Token
4. 保存账号信息到数据库
5. 自动加载到账号池中

这样可以快速批量创建和管理大量 Anything AI 账号。

### Microsoft Graph API 配置（可选）

如果需要使用 Outlook 批量导入功能，需要配置 Microsoft Graph API：

```env
MS_CLIENT_ID=your_client_id
MS_CLIENT_SECRET=your_client_secret
MS_TENANT_ID=common
MS_REDIRECT_URI=http://localhost:8000/admin/oauth/callback
```

在 Azure Portal 创建应用注册并获取凭证。

## 数据库

### SQLite (默认)

数据存储在 `data/anything_proxy.db`，适合单机部署。

### PostgreSQL (推荐生产环境)

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
AUTO_MIGRATE_SQLITE_TO_POSTGRES=true
```

首次启动时会自动从 SQLite 迁移数据。

## Redis (可选)

多进程部署时推荐使用 Redis 共享运行时状态：

```env
REDIS_URL=redis://localhost:6379/0
REDIS_PREFIX=anything_proxy
```

## 部署

### Docker

```bash
docker build -t anything-proxy .
docker run -d -p 8000:8000 --env-file .env anything-proxy
```

### Systemd

创建 `/etc/systemd/system/anything-proxy.service`：

```ini
[Unit]
Description=Anything AI Proxy
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/anything-proxy
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable anything-proxy
sudo systemctl start anything-proxy
```

## 开发

### 项目结构

```
.
├── main.py                 # 应用入口
├── config.py              # 配置管理
├── anything_client.py     # Anything AI 客户端
├── routes/                # API 路由
├── services/              # 业务逻辑
├── database/              # 数据库模型和迁移
├── static/                # 静态文件
└── templates/             # HTML 模板
```

### 运行测试

```bash
pytest tests/
```

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

## 免责声明

本项目仅供学习交流使用，请遵守 Anything AI 的服务条款。使用本项目产生的任何后果由使用者自行承担。
