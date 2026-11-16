FROM nginx:alpine

ENV PORT=80

# Template will be substituted with $PORT by nginx entrypoint
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY an_avatar.conf /etc/nginx/conf.d/an_avatar.conf

# Static branding assets
COPY logo_bookingcare.svg /usr/share/nginx/html/logo_bookingcare.svg
COPY logo_bookingcare.png /usr/share/nginx/html/logo_bookingcare.png
COPY favicon_32.png /usr/share/nginx/html/favicon.ico

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

