#!/bin/bash
# ============================================
# 开品项目管理系统 - Linux 服务器一键部署脚本
# 在目标 Linux 服务器上以 root 执行:
#   chmod +x deploy.sh && ./deploy.sh
# ============================================
set -e

APP_DIR="/opt/kaipin"
VENV_DIR="$APP_DIR/venv"
LOG_DIR="/var/log/kaipin"

echo "======================================"
echo "  开品项目管理系统 - 服务器部署"
echo "======================================"

# ---------- 1. 系统依赖 ----------
echo ""
echo "[1/7] 安装系统依赖..."
if command -v apt-get &>/dev/null; then
    apt-get update
    apt-get install -y python3 python3-venv python3-dev \
        mysql-server libmysqlclient-dev nginx supervisor 2>/dev/null || true
elif command -v yum &>/dev/null; then
    yum install -y python3 python3-pip python3-devel \
        mysql-server mysql-devel nginx 2>/dev/null || true
fi

# ---------- 2. 创建目录 ----------
echo "[2/7] 创建目录..."
mkdir -p "$APP_DIR" "$LOG_DIR"
chown -R "$SUDO_USER:$SUDO_USER" "$APP_DIR" 2>/dev/null || true

# ---------- 3. 部署代码 ----------
echo "[3/7] 部署代码..."
# 假设 kaipin/ 目录已经在当前路径下
if [ -d "./kaipin" ]; then
    rsync -av --delete ./kaipin/ "$APP_DIR/" 2>/dev/null || cp -rf ./kaipin/. "$APP_DIR/"
else
    echo "ERROR: 找不到 kaipin/ 目录，请确保在项目父目录执行此脚本"
    exit 1
fi

# ---------- 4. 配置 .env ----------
echo "[4/7] 配置环境变量..."
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        echo "  .env 已从模板创建，请用 vi $APP_DIR/.env 修改密钥和数据库密码"
    else
        echo "  WARNING: 无 .env.example，请手动创建 $APP_DIR/.env"
    fi
else
    echo "  .env 已存在，跳过"
fi

# ---------- 5. Python 虚拟环境 ----------
echo "[5/7] 创建 Python 虚拟环境..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  OK"

# ---------- 6. 导入数据库 ----------
echo "[6/7] 配置数据库..."
# 创建 MySQL 数据库和用户（如果你还没有的话）
# 按 .env 里的 DB_USER/DB_PASSWORD/DB_NAME 创建
read -p "MySQL root 密码: " -s MYSQL_ROOT_PWD
echo ""

DB_NAME=$(grep DB_NAME "$APP_DIR/.env" | cut -d= -f2)
DB_USER=$(grep DB_USER "$APP_DIR/.env" | cut -d= -f2)
DB_PASS=$(grep DB_PASSWORD "$APP_DIR/.env" | cut -d= -f2)

mysql -u root -p"$MYSQL_ROOT_PWD" <<SQL
CREATE DATABASE IF NOT EXISTS $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
echo "  数据库已创建"

# 导入数据
if [ -f "$APP_DIR/deploy/kaipin_dump.sql" ]; then
    mysql -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" < "$APP_DIR/deploy/kaipin_dump.sql"
    echo "  数据已导入"
fi

# Django migrate（确保 migration 记录同步）
"$VENV_DIR/bin/python" "$APP_DIR/manage.py" migrate --fake-initial 2>/dev/null || true
echo "  migrate OK"

# ---------- 7. 启动服务 ----------
echo "[7/7] 配置并启动服务..."

# 静态文件收集
"$VENV_DIR/bin/python" "$APP_DIR/manage.py" collectstatic --noinput -q
echo "  静态文件已收集"

# systemd service
cp "$APP_DIR/deploy/kaipin.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable kaipin --now

# nginx
if [ -f "$APP_DIR/deploy/nginx.conf" ]; then
    cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/kaipin
    ln -sf /etc/nginx/sites-available/kaipin /etc/nginx/sites-enabled/
    # 删掉默认站点
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx
    echo "  nginx 已配置"
fi

echo ""
echo "======================================"
echo "  部署完成！"
echo "  访问: http://$(hostname -I | awk '{print $1}')"
echo "======================================"
