#!/bin/bash
# Reset app_user password and grant it trade_history access
mysql -u root -p'#JAYA1708!!' << 'SQLEOF'
ALTER USER 'app_user'@'localhost' IDENTIFIED WITH mysql_native_password BY 'SecurePassword123!';
GRANT ALL PRIVILEGES ON trade_history.* TO 'app_user'@'localhost';
FLUSH PRIVILEGES;
SQLEOF
echo "Done. Verifying..."
mysql -u root -p'#JAYA1708!!' -e "SHOW GRANTS FOR 'app_user'@'localhost';" 2>&1
echo "Testing app_user login..."
mysql -u app_user -p'SecurePassword123!' trade_history -e "SHOW TABLES;" 2>&1
