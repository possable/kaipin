#!/bin/bash
# certbot --manual-auth-hook：把验证值写出来，并等待人工把 TXT 记录加到阿里云 DNS
echo "$CERTBOT_VALIDATION" > /tmp/certbot-txt-value.txt
# 等待 /tmp/certbot-txt-ready.flag 出现（由操作者确认记录已生效后创建）
while [ ! -f /tmp/certbot-txt-ready.flag ]; do
  sleep 5
done
exit 0
