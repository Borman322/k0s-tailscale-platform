# k0s-tailscale-platform

[English](README.md) · **Русский**

Небольшая Kubernetes-платформа, которая поднимается на паре дешёвых виртуалок одной
командой: **k0s** поверх **Tailscale**, с защитой по умолчанию, плюс пример приложения —
**jobmon**, сборщик вакансий (API систем найма и доски удалёнки → Postgres → Grafana по HTTPS).

```
                        tailnet (Tailscale / WireGuard)
 ┌─────────────────────────┐   Calico VXLAN   ┌──────────────────────────────┐
 │ control plane           │◄────────────────►│ worker(s)                    │
 │ k0s controller + etcd   │                  │ k0s worker                   │
 │ taint NoSchedule        │                  │ jobmon: db · collector ·     │
 │ ufw: 22/tcp, 41641/udp  │                  │ grafana · tailscale-proxy    │
 └─────────────────────────┘                  └──────────────────────────────┘

 https://grafana.<tailnet>.ts.net  — сертификат Let's Encrypt от Tailscale operator,
                                     открывается только с устройств tailnet
```

- **Одна команда:** `make deploy` — control plane, воркеры, аддоны, приложение. Можно перезапускать.
- **Или кнопка:** GitHub Actions → *deploy* → *Run workflow*.
- **Закрыто по умолчанию:** в интернет смотрят только SSH и UDP-порт Tailscale; кластер
  общается через tailnet; Grafana получает HTTPS без своего домена и открытых портов.

## Что внутри

| Путь | Что это |
|---|---|
| `Makefile` | все команды: `make help` |
| `site.yml` | всё по порядку: `controller.yml` → `workers.yml` → `addons.yml` → `app.yml` |
| `roles/` | `base` (пакеты, sysctl, DNS), `tailscale`, `ufw`, `k0s_*`, `cluster_addons`, `jobmon` |
| `addons/` | local-path-provisioner v0.0.37 (тома на диске узла) |
| `app/collector/` | сборщик и его `Dockerfile`; что искать — `profile.yml` и `companies.yml` |
| `app/k8s/`, `app/kustomization.yaml` | jobmon для Kubernetes (база; значения установки подставляет `roles/jobmon`) |
| `app/docker-compose.yml` | jobmon на одном хосте без Kubernetes |
| `app/db/`, `app/grafana/` | схема БД, источник данных и дашборд Grafana |
| `.github/workflows/` | `collector-image` — образ в ghcr; `deploy` — развёртывание кнопкой |
| `group_vars/all.yml` | настройки: версии, сети, домен tailnet, образ collector |
| `inventory.example.ini`, `vars/secrets.example.yml` | образцы; настоящие файлы в git не попадают |

## Что нужно

**Хосты:** от двух, Ubuntu 22.04/24.04 x86_64, от 2 GB RAM, SSH под `root` по ключу.
Один станет control plane, остальные — воркерами.

**Tailscale** (админка tailnet, один раз):

1. *Settings → Keys:* reusable + pre-approved auth key → `tailscale_authkey`.
2. *Access controls:* в `tagOwners` добавить
   `"tag:k8s-operator": []` и `"tag:k8s": ["tag:k8s-operator"]`.
3. *DNS:* включить MagicDNS и **HTTPS Certificates**. Суффикс сети (`tailXXXX.ts.net`) —
   это `tailnet_domain`.
4. *Trust credentials:* OAuth client, **write** на General/Services, Devices/Core,
   Keys/Auth Keys, тег `tag:k8s-operator` → `tailscale_operator_oauth_client_id` / `_secret`.
   Secret показывается один раз.

**Откуда запускаете:** `ansible-core` ≥ 2.18, `helm`, `make`, `openssl`. В tailnet эту
машину добавлять не нужно: хосты доступны по SSH по публичным адресам.

## Развернуть с нуля

```bash
git clone https://github.com/Borman322/k0s-tailscale-platform && cd k0s-tailscale-platform
make init        # коллекции Ansible + inventory.ini, vars/secrets.yml (пароли случайные)
```

Затем заполнить три файла:

- `inventory.ini` — адреса хостов (`[first_controller]` и `[new_workers]`);
- `vars/secrets.yml` — ключи Tailscale из шагов выше;
- `group_vars/all.yml` — `tailnet_domain` и `jobmon_collector_image`.

```bash
make deploy      # кластер, аддоны и jobmon; повторный запуск ничего не ломает
```

Через несколько минут Grafana открывается по `https://grafana.<tailnet_domain>`
(логин `admin`, пароль — `jobmon_grafana_password`) с устройства в том же tailnet.

### Образ collector

Залейте свою копию на GitHub: workflow **collector-image** соберёт образ и положит в
`ghcr.io/<owner>/<repo>-collector` с тегами `sha-<коммит>` и `latest`. Нужный тег он пишет в
summary запуска — его и ставить в `jobmon_collector_image`, затем `make app`.

Новый пакет в ghcr может оказаться приватным. Либо сделать его публичным (*Package settings →
Change visibility*), либо заполнить `ghcr_pull_user` и `ghcr_pull_token` (токен только с
`read:packages`) в `vars/secrets.yml`.

## Настроить поиск под себя

Сборщик ходит напрямую в доски компаний — Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Recruitee, Personio, Teamtailor, BambooHR, Rippling, Gem, Breezy — и в доски
удалёнки (Himalayas, Jobicy, RemoteOK, We Work Remotely, Arbeitnow, HN «Who is hiring?»).
Каждой вакансии ставится балл, сохраняются те, что выше `min_score`.

- `app/collector/profile.yml` — **что считать подходящим:** ключевые слова с весами,
  шаблоны заголовков (подходит / не подходит), необязательный список слов `domain` (на широких
  досках оставляет только вакансии из вашей области). В примере — профиль SRE / Platform / DevOps.
- `app/collector/companies.yml` — **где искать:** компании с их системой найма и slug
  (`ats: probe` — система определится сама) и какие доски читать.
- `python discover.py` — подбирает систему найма и slug для названий из `seed_companies.txt`
  и дописывает их в `companies.yml`.

Попробовать локально без базы:

```bash
cd app/collector && pip install -r requirements.txt
python collector.py --dry-run --top 20
```

Вакансии с ограничением по региону (`us-only`, `eu-auth`) или только в офисе (`onsite`) не
выкидываются, а помечаются — на дашборде это отдельная колонка.

## В пару кликов из GitHub

Workflow **deploy** запускает тот же `make <цель>` на раннере GitHub.

1. Ключ для деплоя: `ssh-keygen -t ed25519 -f deploy_key -N ''`. Публичную часть
   (`deploy_key.pub`) добавить в `base_ssh_authorized_keys` в `group_vars/all.yml` и один раз
   выполнить `make cluster` со своей машины — ключ разойдётся по всем хостам.
2. *Settings → Secrets and variables → Actions*, три секрета:
   - `SSH_PRIVATE_KEY` — содержимое `deploy_key`;
   - `INVENTORY_INI` — содержимое `inventory.ini`;
   - `SECRETS_YML` — содержимое `vars/secrets.yml`.
3. *Actions → deploy → Run workflow*, выбрать цель: `app`, `deploy`, `cluster`, `workers`,
   `addons`, `status` или `backup` (дамп БД придёт артефактом запуска).

Поменяли `inventory.ini` или `vars/secrets.yml` — обновите и секреты:
`gh secret set INVENTORY_INI < inventory.ini`.

## Каждый день

| Команда | Что делает |
|---|---|
| `make status` | узлы и поды |
| `make app` | обновить jobmon (после правки манифестов или тега образа) |
| `make workers` | присоединить новые хосты из `[new_workers]` |
| `make backup` | `pg_dump` базы в `./backups/` |
| `make check` | проверить синтаксис плейбуков |

`kubectl` работает только на контроллере (API слушает tailnet-адрес):

```bash
ssh root@<controller> 'k0s kubectl -n jobmon get pods'
# внеплановый проход сборщика
ssh root@<controller> 'k0s kubectl -n jobmon create job --from=cronjob/collector collector-manual-$(date +%s)'
# восстановить дамп
ssh root@<controller> 'k0s kubectl -n jobmon exec -i db-0 -- pg_restore -U jobs -d jobs --clean --if-exists --no-owner' < backups/jobmon-<время>.dump
```

**Добавить воркер:** дописать хост в `[new_workers]` → `make workers`. Уже присоединённые
хосты не трогаются. С `--limit` контроллер должен оставаться в списке: он выдаёт токен.

## Без Kubernetes

На одном хосте с Docker:

```bash
cd app && docker compose up -d --build     # пароли — в app/.env (создаёт make init)
```

Grafana по умолчанию слушает только `127.0.0.1:3000` (снаружи — `ssh -L 3000:localhost:3000`);
`GRAFANA_BIND=0.0.0.0` в `.env` открывает её наружу.

## Решения и почему

- **Calico VXLAN, а не kube-router.** Tailscale пропускает только TCP, UDP и ICMP, а
  kube-router гонит трафик подов между узлами в IPIP (IP-протокол 4) — tailscaled его молча
  режет (`Drop: IPProto-4 … unknown-protocol-4`), при этом узлы `Ready`. VXLAN — это UDP;
  MTU 1230 под 1280 у `tailscale0`. CNI на живом k0s-кластере не меняется.
- **`Ready` узла почти ничего не доказывает.** Плейбуки ждут системные поды на каждом узле:
  calico и konnectivity не поднимутся, если сеть подов через tailnet не работает.
- **Control plane виден в `kubectl get nodes`, но без нагрузки:** kubelet есть, taint
  `NoSchedule` держит приложения на воркерах (режимы — `roles/k0s_controller/defaults/main.yml`).
- **Swap оставлен** (2-гиговым хостам он нужен), kubelet получает `--fail-swap-on=false`.
- **Открыты только SSH и UDP 41641**, весь трафик кластера идёт через `tailscale0`.
- **jobmon:** Pod Security `restricted` для его namespace (ничто не работает от root, Postgres
  тоже), NetworkPolicy (к Postgres — только collector и Grafana, к Grafana — только прокси
  Tailscale), тома на `local-path-retain` (удаление PVC не удаляет данные), пароли в Secret.
- **Секреты не в git.** GitHub сканирует репозитории на ключи Tailscale и сообщает о них в
  Tailscale, а тот сразу их отзывает. `vars/secrets.yml` и `inventory.ini` в `.gitignore`,
  Actions берут их из секретов репозитория.
