#!/bin/bash
# certbot --manual-cleanup-hook：验证结束后清理标记文件
rm -f /tmp/certbot-txt-ready.flag /tmp/certbot-txt-value.txt
exit 0
