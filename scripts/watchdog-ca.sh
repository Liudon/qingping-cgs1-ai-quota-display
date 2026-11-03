export SSL_CERT_FILE=/data/custom/ai-quota-display/ISRG-Root-X1.pem
if ! mount | grep -q ' on /etc/ssl/certs '; then mount -o bind /data/custom/ai-quota-display/qt-certs /etc/ssl/certs; fi
