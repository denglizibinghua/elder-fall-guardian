#!/bin/bash
# Pi 侧清理 + 部署：tools/ 整理、删除废弃 dashboard、重启两个服务
# 用法：bash /home/admin/food_project/deploy_cleanup.sh
set -x

cd /home/admin/food_project || exit 1

# 1) 6 个硬件调试脚本收进 tools/
mkdir -p tools
mv -f test.py test_radar.py test_ld_radars.py test_max30102.py sensor_test.py loopback_test.py tools/ 2>/dev/null
echo "=== tools/ ==="
ls -1 tools/

# 2) 删除废弃看板 dashboard.py（已被 app.py 取代）
sudo -n systemctl disable --now dashboard.service 2>&1 | tail -3
rm -f dashboard.py dashboard.log
sudo -n rm -f /etc/systemd/system/dashboard.service
sudo -n systemctl daemon-reload

# 3) 重启服务（food_project=main.py 采集，webapp=app.py 前端）
sudo -n systemctl restart food_project.service
sudo -n systemctl restart webapp.service

sleep 4
echo "=== service status ==="
systemctl is-active food_project.service
systemctl is-active webapp.service

echo "=== webapp http ==="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8080/

echo "=== root py files ==="
ls -1 *.py
