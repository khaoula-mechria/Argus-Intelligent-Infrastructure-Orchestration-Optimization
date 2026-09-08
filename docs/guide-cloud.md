# Guide cloud — Argus sur un vrai compte AWS

Ce guide couvre l'utilisation d'Argus contre un compte AWS réel : les
identifiants, les permissions IAM minimales, ce que ça coûte, et comment ne pas
se faire mal.

Si vous découvrez l'outil, **commencez par [`guide-local.md`](guide-local.md)** :
tout le graphe, l'ordonnancement et le dashboard s'exercent sans compte, et
LocalStack permet même de vrais `create_stack`. Revenez ici quand vous voulez
déployer pour de bon.

---

## Sommaire

1. [Avant de commencer](#avant-de-commencer)
2. [Identifiants](#identifiants)
3. [Permissions IAM](#permissions-iam)
4. [Ce que ça coûte](#ce-que-ça-coûte)
5. [Déployer, prudemment](#déployer-prudemment)
6. [Lire un échec](#lire-un-échec)
7. [Module 2 — l'analyse de dimensionnement](#module-2--lanalyse-de-dimensionnement)
8. [Module 3 — l'explication](#module-3--lexplication)
9. [Plusieurs comptes, plusieurs environnements](#plusieurs-comptes-plusieurs-environnements)
10. [Argus en CI/CD](#argus-en-cicd)
11. [Nettoyer](#nettoyer)
12. [Autres fournisseurs cloud](#autres-fournisseurs-cloud)
13. [Liste de vérification avant un premier déploiement réel](#liste-de-vérification-avant-un-premier-déploiement-réel)

---

## Avant de commencer

Trois garde-fous sont dans l'outil, utilisez-les :

| Garde-fou | Ce qu'il fait |
|---|---|
| `--plan-only` | Calcule et affiche le plan, s'arrête avant tout appel modifiant |
| `--dry-run` | Déroule le plan avec des déploiements simulés, aucun appel cloud |
| pré-validation | `validate_template` sur chaque template avant la première création |
| confirmation | Affiche la cible (compte, région, endpoint) et demande `y` |

Et un qui n'y est pas : **Argus n'a aucun chemin de code qui supprime quoi que
ce soit.** Pas de `delete_stack`, pas de `terraform destroy`, pas de
redimensionnement automatique. Le Module 3 imprime des commandes ; c'est vous
qui les lancez.

---

## Identifiants

Argus utilise boto3, donc la chaîne de résolution habituelle s'applique. Trois
façons, de la plus recommandée à la moins :

### 1. IAM Identity Center (SSO) — recommandé

```bash
aws configure sso --profile argus-dev
aws sso login --profile argus-dev

argus deploy infrastructure/cloudformation --profile argus-dev --region eu-west-3
```

Identifiants temporaires, révocables, aucun secret sur disque.

### 2. Un profil nommé

`~/.aws/credentials` :

```ini
[argus-dev]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = eu-west-3
```

```bash
argus deploy infrastructure/cloudformation --profile argus-dev
```

### 3. Variables d'environnement

```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=eu-west-3

argus deploy infrastructure/cloudformation
```

En CI, préférez OIDC (rôle assumé sans secret de longue durée) à des clés
stockées en variables de dépôt.

### Vérifier sur quel compte vous êtes

**Faites-le avant chaque premier déploiement dans une session.**

```bash
aws sts get-caller-identity --profile argus-dev
```

Argus affiche aussi sa cible avant de demander confirmation :

```
target: AWS (profile=argus-dev, region=eu-west-3)
```

Si cette ligne dit `AWS` alors que vous pensiez être sur LocalStack, arrêtez-vous.

### Précédence

CLI (`--region`, `--profile`, `--endpoint-url`) > `argus.yaml` > variables
d'environnement > profil par défaut.

---

## Permissions IAM

Deux politiques, volontairement séparées.

### Lecture seule : planifier et analyser

[`docs/iam/argus-plan-and-observe.json`](iam/argus-plan-and-observe.json)

Suffit pour `argus graph`, `argus deploy --plan-only`, `argus deploy --dry-run`
et `argus optimize`. **Elle ne peut rien créer, modifier ni supprimer.** C'est
la politique à donner par défaut, y compris à vous-même au quotidien.

À noter : `argus graph` ne demande **aucune** permission AWS — le graphe est
déduit du texte des templates. Les entrées CloudFormation de cette politique
servent à `--validate` et à la lecture de statut.

### Déploiement : à utiliser avec un rôle de service

[`docs/iam/argus-deploy.json`](iam/argus-deploy.json)

**Le point important, et il est souvent raté.** Par défaut, CloudFormation crée
les ressources avec les permissions de *l'identité appelante*. Déployer les 12
stacks de ce dépôt demanderait donc à votre identité `ec2:*`, `ecs:*`,
`iam:CreateRole`, `elasticloadbalancing:*`… c'est-à-dire pratiquement
l'administration du compte, en permanence, y compris quand vous ne déployez pas.

La bonne pratique est un **rôle de service CloudFormation** : CloudFormation
l'assume pour toucher aux ressources, et votre identité n'a plus besoin que des
actions au niveau de la stack, plus `iam:PassRole` sur ce seul rôle.

```bash
# 1. Le rôle que CloudFormation assumera
aws iam create-role --role-name ArgusCloudFormationServiceRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow",
      "Principal":{"Service":"cloudformation.amazonaws.com"},
      "Action":"sts:AssumeRole"}]}'

# 2. Les permissions dont il a besoin : celles des ressources de VOS templates.
#    Commencez large en dev, resserrez ensuite avec Access Analyzer.
aws iam attach-role-policy --role-name ArgusCloudFormationServiceRole \
  --policy-arn arn:aws:iam::aws:policy/PowerUserAccess

# 3. Votre identité, elle, ne reçoit que argus-deploy.json
aws iam put-user-policy --user-name votre-utilisateur \
  --policy-name ArgusDeploy \
  --policy-document file://docs/iam/argus-deploy.json
```

Remplacez `111122223333` et `eu-west-3` dans le fichier par les vôtres.

> **Limite connue et assumée.** Argus ne passe pas lui-même de `RoleARN` à
> `create_stack`. Associez le rôle de service à chaque stack au premier
> déploiement (console, ou `aws cloudformation create-stack --role-arn`), ou
> imposez-le par une condition IAM. Sans rôle de service, `argus-deploy.json`
> **ne suffit pas** : l'identité aura aussi besoin des permissions de chaque
> type de ressource — c'est le cas que ce guide déconseille.

La politique de déploiement contient un `Deny` explicite sur `DeleteStack`.
Argus n'a pas de chemin de suppression ; ce `Deny` fait qu'une évolution future,
ou une commande `aws` mal tapée avec cette identité, n'en aura pas non plus.

---

## Ce que ça coûte

Déployer les 12 stacks de ce dépôt **crée des ressources facturées à l'heure**,
que vous les utilisiez ou non.

| Ressource | Ordre de grandeur | Remarque |
|---|---|---|
| NAT Gateway | ~32 $/mois l'unité | `NatGatewayStrategy=single` (défaut) en crée 1, `ha` en crée 2 — c'est documenté dans [`vpc.yml`](../infrastructure/cloudformation/vpc.yml) |
| Application Load Balancer | ~16-20 $/mois | + le trafic traité |
| Tâches ECS Fargate | à la seconde | Selon `DesiredCount`, CPU et mémoire |
| Secrets Manager | 0,40 $/secret/mois | 2 secrets |
| ECR, CloudWatch Logs, S3 | quelques centimes | Selon volume |

**Le NAT Gateway est le premier poste, et il tourne même sans trafic.** Sur un
compte de test, c'est la ressource à supprimer en premier quand vous avez fini.

Ces chiffres sont des ordres de grandeur pour une région européenne ;
vérifiez la [tarification AWS](https://aws.amazon.com/pricing/) pour la vôtre.
Pensez à poser un budget avant :

```bash
aws budgets create-budget --account-id 111122223333 \
  --budget '{"BudgetName":"argus-dev","BudgetLimit":{"Amount":"50","Unit":"USD"},
             "TimeUnit":"MONTHLY","BudgetType":"COST"}'
```

---

## Déployer, prudemment

### Étape 1 — le plan, sans rien toucher

```bash
argus deploy infrastructure/cloudformation \
  --profile argus-dev --region eu-west-3 \
  -p ProjectName=taskmanager -p Environment=dev \
  --plan-only
```

Relisez :

* le nombre d'unités et les vagues ;
* le **chemin critique** — c'est la limite structurelle ;
* les **arêtes déclarées dans `argus.yaml`**, listées séparément : ce sont les
  ordres qu'un humain a affirmés, pas ceux qu'Argus a déduits ;
* les éventuels **imports non satisfaits** : ils doivent déjà exister sur le
  compte, sinon l'ordre est incomplet.

### Étape 2 — déployer

```bash
argus deploy infrastructure/cloudformation \
  --profile argus-dev --region eu-west-3 \
  -p ProjectName=taskmanager -p Environment=dev \
  --max-parallel 4 \
  --report-json run.json
```

Ce qui se passe :

```
pre-flight: 12 template(s) validated

target: AWS (profile=argus-dev, region=eu-west-3)
Deploy 12 unit(s) to AWS? [y/N]:

deploying 12 unit(s), strategy=rolling
Ctrl-C stops scheduling; units already started are left to finish.
  vpc                      IN_PROGRESS
  ecr                      IN_PROGRESS
  ...
```

**`--max-parallel`** : la concurrence maximale utile est affichée par
`argus graph` (`peak concurrency`) — au-delà, vous ne gagnez rien et vous
augmentez le risque de throttling. Les clients ont des retries adaptatifs et le
polling est décorrélé, mais 4 à 6 reste un bon réglage.

**`--strategy`** : `rolling` par défaut. `waves` existe pour comparer, ou si
vous voulez une barrière franche entre générations (par exemple pour inspecter
l'état entre deux vagues).

### Étape 3 — lire le rapport

```
strategy                    : rolling
wall clock                  : 412.6s
sequential equivalent       : 903.1s
critical path (the floor)   : 388.0s  [vpc -> alb -> ecs-service -> pipeline -> observability]
saved vs sequential         : 490.5s (x2.19)
efficiency vs the floor     : 94%
```

> Ce bloc-ci est un **exemple de format**, pas une mesure réelle : ce dépôt n'a
> pas été déployé sur un vrai compte pendant l'écriture du guide. Les chiffres
> vérifiés en dry-run sont dans [`guide-local.md`](guide-local.md).

L'« efficacité » compare l'horloge murale au chemin critique, c'est-à-dire au
plancher absolu. En dessous de 100 %, du temps a été passé à attendre autre
chose qu'une vraie dépendance : la barrière de vagues, ou le plafond
`--max-parallel`.

### Interrompre

`Ctrl-C` **arrête l'ordonnanceur, pas AWS.** Les unités déjà lancées vont au
bout — c'est délibéré : tuer le processus pendant un `create_stack` n'arrête pas
CloudFormation, ça ne fait que perdre sa trace. Le rapport final marque les
unités jamais tentées comme `SKIPPED / not attempted: run cancelled`.

---

## Lire un échec

Quand une stack échoue, CloudFormation met dans `StackStatusReason` un message
inutile :

```
The following resource(s) failed to create: [EcsCluster].
```

Argus remonte l'historique des événements jusqu'à la **première** vraie panne,
en écartant les ressources simplement annulées par effet de bord :

```
FAILED ecs-cluster: EcsCluster (AWS::ECS::Cluster): You have reached the limit of clusters per account
```

Ensuite :

* les unités qui dépendaient de la stack en échec sont marquées `SKIPPED`,
  **transitivement** — elles ne sont pas tentées, parce qu'elles échoueraient de
  toute façon sur un export manquant et ajouteraient une seconde erreur
  trompeuse ;
* les unités indépendantes continuent ;
* le code de sortie est `1`, et `run.json` contient le détail par unité.

Corrigez, puis relancez la même commande : Argus fait `update_stack` sur ce qui
existe déjà et `create_stack` sur le reste. Une stack sans changement renvoie
`no changes` et n'est pas une erreur.

> **Une stack en `ROLLBACK_COMPLETE` ne peut pas être mise à jour.**
> CloudFormation impose de la supprimer d'abord. Argus ne supprime rien : faites
> `aws cloudformation delete-stack --stack-name <nom>` vous-même, puis relancez.

---

## Module 2 — l'analyse de dimensionnement

```bash
argus optimize --service taskmanager-dev-service \
  --profile argus-dev --region eu-west-3
```

Aucune modification : que des lectures CloudWatch et Compute Optimizer.

```
ecs-service taskmanager-dev-service in eu-west-3
window: last 14 days

metrics (p95 over the window):
  CPUUtilization           p95=   11.30 Percent  avg=    8.10  max=   47.00  (336 datapoints)
  MemoryUtilization        p95=   22.80 Percent  avg=   19.40  max=   31.00  (336 datapoints)

Argus rule: over-provisioned
  CPUUtilization p95=11.3%, MemoryUtilization p95=22.8%, every metric below the 40.0% threshold.
  current : cpu=1024, memory=2048
  proposed: cpu=1024, memory=3072

AWS Compute Optimizer: over-provisioned
  proposed: cpu=512, memory=1024
  estimated monthly saving: $18.40

verdict: agree: over-provisioned
```

Les deux verdicts sont **juxtaposés, pas réconciliés**. Compute Optimizer exige
~14 jours d'historique et se tait en dessous ; la règle locale produit toujours
quelque chose. Leur désaccord est une information.

### Deux prérequis souvent oubliés

1. **Compute Optimizer doit être activé sur le compte** — ce n'est pas le cas
   par défaut :
   ```bash
   aws compute-optimizer update-enrollment-status --status Active
   ```
   Sans ça, Argus le signale dans les `notes` et continue avec sa seule règle.

2. **`MemoryUtilization` sur ECS demande Container Insights.** Sans lui, la
   métrique n'existe pas et Argus dira `no datapoints` plutôt que d'inventer.
   (Les stacks de ce dépôt l'activent déjà — voir `ecs-cluster.yaml`.)

### Les autres types

```bash
argus optimize --resource i-0123456789abcdef0 --type ec2-instance
argus optimize --resource mydb --type rds-instance
argus optimize --resource vol-0123456789abcdef0 --type ebs-volume
```

Pour `ebs-volume`, Argus lit les métriques et relaie la recommandation AWS mais
**n'applique pas sa règle** : dimensionner un volume dépend des IOPS et du
débit, pas de pourcentages d'utilisation, et prétendre le contraire produirait
une bêtise assurée.

---

## Module 3 — l'explication

```bash
export ANTHROPIC_API_KEY=sk-ant-...

argus explain --service taskmanager-dev-service --region eu-west-3
argus explain --report rapport.json --ask "512 CPU suffit pour un pic de trafic ?"
```

Le modèle par défaut est `claude-opus-5`. Le system prompt exige que chaque
affirmation soit ancrée dans un chiffre du rapport et impose de dire platement
quand les données sont trop maigres.

```bash
argus explain --report rapport.json --apply
```

`--apply` **n'applique rien**. Il imprime les commandes qu'un humain devrait
lancer, avec leurs effets de bord annoncés (un `modify-db-instance` redémarre
l'instance, un redimensionnement EC2 impose un stop/start qui change l'IP
publique). Argus n'a aucun chemin de code qui redimensionne une ressource — et
c'est justement à cette lecture qu'une mauvaise recommandation se fait attraper.

Sur ce dépôt, la modification durable n'est d'ailleurs pas une révision de task
definition à la main : c'est `task-manager/taskdef.template.json` et
`ecs-task-definition.yaml`. Le plan imprimé le rappelle.

---

## Plusieurs comptes, plusieurs environnements

Un `argus.yaml` par environnement :

```yaml
# argus.prod.yaml
backend: cloudformation
path: infrastructure/cloudformation
profile: argus-prod
region: eu-west-1
stack_name_prefix: prod-
max_parallel: 4
parameters:
  ProjectName: taskmanager
  Environment: prod
  NatGatewayStrategy: ha
depends_on:
  alb: [vpc]
```

```bash
argus deploy infrastructure/cloudformation --config argus.prod.yaml --plan-only
```

`stack_name_prefix` évite les collisions de noms quand plusieurs environnements
partagent un compte. Attention : les **noms d'export** CloudFormation sont
uniques par compte et par région — c'est `Environment` qui les différencie dans
ce dépôt, pas le préfixe de stack.

---

## Argus en CI/CD

Le seul usage que je recommande sans réserve en CI automatique est la
**vérification**, pas le déploiement :

```yaml
- name: Argus — vérifier le graphe
  run: |
    python -m argus.cli graph infrastructure/cloudformation --format json > graph.json
    python -m argus.cli deploy infrastructure/cloudformation --dry-run --report-json run.json
```

Aucun secret, et ça échoue sur un cycle, un nom dupliqué ou un `depends_on`
mort — des erreurs qui autrement ne se voient qu'au déploiement.

Pour un déploiement automatique, il faut `--yes` (qui saute la confirmation).
Ne le faites que sur un environnement non-production, avec un rôle OIDC à
permissions restreintes et une approbation manuelle en amont :

```yaml
- name: Déployer (dev uniquement)
  if: github.ref == 'refs/heads/develop'
  run: |
    python -m argus.cli deploy infrastructure/cloudformation \
      --region eu-west-3 --yes --max-parallel 4 --report-json run.json
```

> Le workflow [`ci.yml`](../.github/workflows/ci.yml) de ce dépôt **n'a pas** été
> modifié : il appartient au pipeline CI/CD d'origine, qu'Argus ne touche pas.

---

## Nettoyer

Argus ne supprime rien. Dans l'ordre **inverse** du graphe :

```bash
# L'ordre inverse du plan, à la main
argus graph infrastructure/cloudformation --format json \
  | python -c "import json,sys; w=json.load(sys.stdin)['waves']; print('\n'.join(' '.join(x) for x in reversed(w)))"

# puis, vague par vague
aws cloudformation delete-stack --stack-name observability --region eu-west-3
aws cloudformation wait stack-delete-complete --stack-name observability --region eu-west-3
# ...
```

Supprimer dans le désordre échoue : CloudFormation refuse de retirer une stack
dont un export est encore importé. C'est la même contrainte que celle qu'Argus
exploite pour ordonner — simplement lue à l'envers.

**Vérifiez que le NAT Gateway est bien parti** : c'est la ressource qui continue
de coûter si une suppression échoue à moitié.

---

## Autres fournisseurs cloud

Argus est aujourd'hui **spécifique à AWS**. L'interface `InfrastructureAdapter`
est neutre, mais les deux adaptateurs livrés ne le sont pas :

* `CloudFormationAdapter` parle CloudFormation via boto3 ;
* `TerraformAdapter` délègue à `terraform`, **et c'est le seul chemin
  utilisable avec un autre fournisseur** : si vos modules Terraform ciblent
  Azure, GCP ou Scaleway, Argus les ordonne et les applique sans rien savoir du
  fournisseur, puisqu'il ne fait qu'appeler `terraform init` / `terraform apply`.

Concrètement, avec un `argus.yaml` déclarant vos modules :

```yaml
backend: terraform
modules:
  - name: network
    path: ./azure-network
  - name: app
    path: ./azure-app
    depends_on: [network]
```

…fonctionne, parce qu'Argus ne regarde jamais à l'intérieur des modules. Les
identifiants sont ceux que le provider Terraform attend (`az login`,
`gcloud auth`, variables d'environnement), pas ceux d'AWS.

En revanche les **Modules 2 et 3 restent AWS-only** : CloudWatch et Compute
Optimizer n'ont pas d'équivalent branché. Ajouter Azure Monitor ou GCP
Recommender demanderait un nouvel adaptateur d'optimisation — ce n'est pas fait,
et je préfère l'écrire ici que de laisser croire l'inverse.

---

## Liste de vérification avant un premier déploiement réel

- [ ] `aws sts get-caller-identity` → c'est bien le compte attendu
- [ ] Un budget AWS est posé sur le compte
- [ ] `argus deploy ... --plan-only` a été relu : vagues, chemin critique, arêtes déclarées, imports non satisfaits
- [ ] `argus deploy ... --dry-run` passe
- [ ] Les paramètres (`ProjectName`, `Environment`, `NatGatewayStrategy`) sont ceux voulus
- [ ] La politique de déploiement est associée à un rôle de service CloudFormation
- [ ] Vous savez comment supprimer ce que vous allez créer (section [Nettoyer](#nettoyer))
- [ ] La ligne `target: AWS (...)` affichée à la confirmation dit bien la bonne région
