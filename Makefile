# Everything goes through Ansible; hosts come from inventory.ini, secrets from vars/secrets.yml.
PLAYBOOK := ansible-playbook

.DEFAULT_GOAL := help
.PHONY: help init check deploy cluster controller workers addons app status backup

help: ## list the commands
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-11s %s\n", $$1, $$2}'

init: ## first run: Ansible collections + inventory.ini, vars/secrets.yml, app/.env from the examples
	ansible-galaxy collection install -r requirements.yml
	./scripts/init.sh

check: ## syntax check of all playbooks
	$(PLAYBOOK) --syntax-check site.yml backup.yml

deploy: ## everything: control plane, workers, add-ons, jobmon
	$(PLAYBOOK) site.yml

cluster: ## control plane + workers
	$(PLAYBOOK) controller.yml workers.yml

controller: ## control plane only
	$(PLAYBOOK) controller.yml

workers: ## join the hosts from [new_workers]
	$(PLAYBOOK) workers.yml

addons: ## local-path storage + Tailscale operator
	$(PLAYBOOK) addons.yml

app: ## deploy or update jobmon
	$(PLAYBOOK) app.yml

status: ## nodes and pods
	ansible first_controller -b -m ansible.builtin.shell \
	  -a 'k0s kubectl get nodes -o wide; echo; k0s kubectl get pods -A -o wide; echo; k0s kubectl -n jobmon get cronjob,ingress'

backup: ## pg_dump of the jobmon database into ./backups/
	$(PLAYBOOK) backup.yml
