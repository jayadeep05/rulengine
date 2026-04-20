#!/bin/bash
# Grant app_user full access to trade_history database
mysql -u root -p'#JAYA1708!!' -e "GRANT ALL PRIVILEGES ON trade_history.* TO 'app_user'@'localhost'; FLUSH PRIVILEGES;" 2>&1
echo "--- Verifying grants ---"
mysql -u root -p'#JAYA1708!!' -e "SHOW GRANTS FOR 'app_user'@'localhost';" 2>&1
echo "--- Verifying app_user can access trade_history ---"
mysql -u app_user -p'#JAYA1708!!' trade_history -e "SHOW TABLES;" 2>&1
