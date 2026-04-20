#!/bin/bash
mysql -u root -p'#JAYA1708!!' -e "SHOW GRANTS FOR 'app_user'@'localhost';" 2>&1
mysql -u root -p'#JAYA1708!!' -e "SHOW DATABASES;" 2>&1
mysql -u root -p'#JAYA1708!!' -e "SELECT user, host FROM mysql.user;" 2>&1
