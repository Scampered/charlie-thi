# The web server: nginx serving the chat page.
# site.conf is not copied in - docker-compose.yml mounts it, so changes to it
# only need "docker compose restart nginx6".
FROM nginx:1.27-alpine
COPY charlie.html logic.js /var/www/html/
