# MikeyRocks//Solo stats pusher as a container, so it survives umbrelOS updates
# (umbrelOS rebuilds /etc on update, which removes systemd timers).
FROM docker:27-cli
RUN apk add --no-cache python3 tzdata
ENV NEON_HOME=/home/umbrel/neon-solo
CMD ["sh", "-c", "while true; do python3 /home/umbrel/neon-solo/neon-push.py; sleep 15; done"]
