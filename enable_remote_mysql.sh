#!/bin/bash
mysql -u root -p'#JAYA1708!!' -e "CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED WITH mysql_native_password BY '#JAYA1708!!'; GRANT ALL PRIVILEGES ON trade_history.* TO 'root'@'%'; FLUSH PRIVILEGES;"
# Bind MySQL to all interfaces
sudo sed -i "s/^bind-address.*/bind-address = 0.0.0.0/" /etc/mysql/mysql.conf.d/mysqld.cnf
sudo systemctl restart mysql
echo "MySQL remote access enabled."
mysql -u root -p'#JAYA1708!!' -e "SELECT user, host FROM mysql.user WHERE user='root';"
