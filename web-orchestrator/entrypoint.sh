#!/bin/bash
# Гарантируем DNS в контейнере (resolv.conf может быть пустым при host networking)
if ! grep -q "^nameserver" /etc/resolv.conf 2>/dev/null; then
    printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" >> /etc/resolv.conf
fi
exec uvicorn main:app --host 0.0.0.0 --port 8080 --no-access-log --log-level warning
