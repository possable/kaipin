# 开品项目管理系统

电商团队新品开发（开品）进度管理系统。

## 快速开始

```bash
# 1. 创建 MySQL 数据库
mysql -u root -p -e "CREATE DATABASE kaipin CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 2. 安装依赖
pip install -r requirements.txt

# 3. 复制 .env.example 为 .env，填入数据库密码、SECRET_KEY 和企业微信参数
cp .env.example .env

# 4. 迁移数据库
python manage.py migrate

# 5. 初始化种子数据
python manage.py seed_data

# 6. 创建超级管理员
python manage.py createsuperuser

# 7. 启动开发服务器
python manage.py runserver 0.0.0.0:8000
```

## 管理员首次使用步骤

1. 登录后，在 Django Admin 中为每个用户设置所属部门、角色（admin/member）和企业微信 UserID
2. 检查"管理 → 流程模板"确认5个阶段的模板正确
3. 点击"管理 → 创建新品"开始第一个品

## 部署到内网服务器

生产环境建议:
- 使用 Gunicorn/uWSGI + Nginx 反向代理
- .env 中设置 DEBUG=False
- .env 中设置随机的 SECRET_KEY（可用 `python manage.py shell -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` 生成）
- 配置 MySQL 字符集 utf8mb4
- 媒体文件定期备份
- .env 文件不要提交到 Git（已加入 .gitignore）
