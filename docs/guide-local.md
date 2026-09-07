# Guide local — Argus sans compte cloud

Tout ce qui suit tourne **sur votre machine**, sans compte AWS, sans clé, et
sans risque de facturation. C'est le guide à suivre pour découvrir l'outil,
pour développer dessus, et pour le faire tourner en CI.

Le guide complémentaire, pour un vrai compte, est
[`guide-cloud.md`](guide-cloud.md).

---

## Sommaire

1. [Ce qui marche à quel niveau](#ce-qui-marche-à-quel-niveau)
2. [Niveau 0 — installation](#niveau-0--installation)
3. [Niveau 1 — le graphe, sans rien installer d'autre](#niveau-1--le-graphe-sans-rien-installer-dautre)
4. [Niveau 2 — le dry-run et la preuve de l'ordonnanceur](#niveau-2--le-dry-run-et-la-preuve-de-lordonnanceur)
5. [Niveau 3 — le dashboard](#niveau-3--le-dashboard)
6. [Niveau 4 — Terraform en local, pour de vrai](#niveau-4--terraform-en-local-pour-de-vrai)
7. [Niveau 5 — LocalStack : de vrais déploiements CloudFormation](#niveau-5--localstack--de-vrais-déploiements-cloudformation)
8. [Ce qui ne peut pas être fait en local](#ce-qui-ne-peut-pas-être-fait-en-local)
9. [Argus en CI](#argus-en-ci)
10. [Dépannage](#dépannage)

---

## Ce qui marche à quel niveau

| Niveau | Prérequis | Ce que vous obtenez |
|---|---|---|
| 1 | Python seul | Graphe, vagues, chemin critique, détection de cycles, imports non satisfaits |
| 2 | Python seul | Dry-run complet, comparaison des deux ordonnanceurs, rapport JSON |
| 3 | Python seul | Dashboard Streamlit, les 3 onglets |
| 4 | + `terraform` | Vrais `terraform apply` sur des ressources locales |
| 5 | + Docker | Vrais `create_stack` CloudFormation contre LocalStack |

Les niveaux 1 à 3 ne demandent **rien d'autre que `pip install -r
requirements.txt`**. Pas de credentials, même factices.

---

## Niveau 0 — installation

```bash
git clone https://github.com/khaoula-mechria/Argus-Intelligent-Infrastructure-Orchestration-Optimization.git
cd Argus-Intelligent-Infrastructure-Orchestration-Optimization

python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate

pip install -r requirements.txt
pip install -e .                 # facultatif, fournit la commande `argus`
```

Sans `pip install -e .`, écrivez `python -m argus.cli` partout où ce guide
écrit `argus`.

Vérifiez :

```bash
python -m pytest tests -q
```

146 tests doivent passer, **sans credentials AWS ni binaire `terraform`**. Si
c'est le cas, l'installation est bonne.

---

## Niveau 1 — le graphe, sans rien installer d'autre

Aucune variable d'environnement, aucune clé.

```bash
argus graph infrastructure/cloudformation
```

```
backend: cloudformation
12 unit(s), 21 dependency edge(s), 7 wave(s)
  wave 1: ecr, ecs-cluster, secrets-manager, vpc
  wave 2: alb, codebuild
  wave 3: iam
  wave 4: ecs-task-definition
  wave 5: ecs-service
  wave 6: pipeline
  wave 7: ecs-autoscaling, observability
critical path (7 units): secrets-manager -> codebuild -> iam -> ecs-task-definition -> ecs-service -> pipeline -> ecs-autoscaling
peak concurrency: 4
```

Trois choses à lire ici :

* **7 vagues pour 12 stacks.** L'ordre séquentiel documenté dans
  [`infrastructure/README.md`](../infrastructure/README.md) se compresse.
* **Le chemin critique fait 7 unités.** C'est la limite *structurelle* : aucun
  ordonnanceur, aucun parallélisme ne peut faire mieux. Pour la raccourcir il
  faut changer l'infrastructure, pas l'outil.
* **Concurrence maximale : 4.** Inutile de régler `max_parallel` au-delà.

Pourquoi ça marche sans AWS : Argus normalise les noms d'export et d'import
des deux côtés avec la même fonction, donc `${ProjectName}-${Environment}-vpc-id`
s'apparie avec lui-même sans qu'aucun paramètre soit résolu. Le graphe est
déduit du texte des templates.

### Autres formats

```bash
argus graph infrastructure/cloudformation --format json   # pour outiller
argus graph infrastructure/cloudformation --format dot    # Graphviz
```

### Sur votre propre projet

```bash
argus graph /chemin/vers/vos/templates --backend cloudformation
```

Argus signale les imports que rien n'exporte :

```
warning: the requirements above are provided by nothing in this project.
```

Ce n'est pas forcément une erreur (l'export peut venir d'un autre projet), mais
c'est exactement là qu'un mauvais ordre se cache.

---

## Niveau 2 — le dry-run et la preuve de l'ordonnanceur

Le dry-run parcourt le plan avec des déploiements **simulés** : aucun appel
réseau, aucun client AWS n'est même construit.

```bash
argus deploy infrastructure/cloudformation --dry-run
```

La sortie se termine par :

```
SIMULATED strategy                    : rolling
SIMULATED wall clock                  : 1.3s
SIMULATED sequential equivalent       : 2.0s
SIMULATED critical path (the floor)   : 1.3s  [vpc -> alb -> ecs-service -> pipeline -> observability]
SIMULATED saved vs sequential         : 0.7s (x1.56)
SIMULATED efficiency vs the floor     : 100%
```

> **`SIMULATED` n'est pas décoratif.** Ces durées sont dérivées du nombre de
> ressources par template, pas d'AWS. Elles démontrent la *structure*, elles ne
> prédisent aucun temps réel.

### Voir le coût de la barrière de vagues

C'est la démonstration la plus parlante de l'outil, et elle est reproductible
chez vous en dix secondes :

```bash
argus deploy infrastructure/cloudformation --dry-run --strategy waves
argus deploy infrastructure/cloudformation --dry-run --strategy rolling
```

| Stratégie | Wall clock | Efficacité vs le plancher |
|---|---|---|
| `waves` | 1.5 s | **83 %** |
| `rolling` | 1.3 s | **100 %** |

Même plan, même graphe, même travail. La différence est entièrement la barrière
entre vagues : avec `waves`, une stack rapide dont la seule dépendance est déjà
finie attend quand même la stack lente de sa génération. `rolling` la démarre
tout de suite.

L'« efficacité » se lit contre le **chemin critique**, pas contre un déroulé
séquentiel. C'est délibéré : la comparaison séquentielle flatte n'importe quelle
implémentation parallèle, alors que le chemin critique est le plancher que rien
ne peut franchir. 100 % veut dire qu'aucune seconde n'a été perdue ailleurs que
sur une vraie dépendance.

### Rapport machine

```bash
argus deploy infrastructure/cloudformation --dry-run --report-json run.json
```

```json
{
  "strategy": "rolling",
  "succeeded": true,
  "wall_clock": 1.276,
  "critical_path_duration": 1.272,
  "efficiency": 0.997,
  "speedup": 1.556,
  "critical_path": ["vpc", "alb", "ecs-service", "pipeline", "observability"],
  "units": [{"name": "ecr", "status": "COMPLETE", "duration": 0.071, "detail": "simulated"}]
}
```

---

## Niveau 3 — le dashboard

```bash
argus dashboard
```

Ouvre `http://localhost:8501`. Toujours aucun credential requis tant que vous
restez en dry-run.

* **Onglet Deployment** : le graphe coloré par statut, groupé par vague. Le
  sélecteur *Strategy* permet de rejouer la comparaison ci-dessus en voyant les
  nœuds s'allumer. Le dry-run est activé par défaut.
* **Onglet Optimization** : demande AWS, voir le guide cloud.
* **Onglet Explanation** : demande une clé Anthropic.

Le champ **Endpoint** en haut de page sert au niveau 5.

Pour voir un échec et la propagation `SKIPPED`, pointez le chemin sur un dossier
de templates dont un import n'est satisfait par personne : le bandeau jaune
apparaît et les unités concernées sont signalées.

---

## Niveau 4 — Terraform en local, pour de vrai

Le dépôt contient un projet exemple bâti sur `terraform_data`, une ressource
**intégrée à Terraform** : aucun provider à télécharger, aucun compte, aucun
appel réseau. Les `apply` sont donc réels.

### Installer Terraform

```bash
# macOS
brew install terraform
# Linux (Debian/Ubuntu) : voir developer.hashicorp.com/terraform/install
# Windows
winget install HashiCorp.Terraform
```

### Le cas multi-modules (là où Argus sert)

```bash
argus graph examples/terraform --backend terraform
```

```
backend: terraform
2 unit(s), 1 dependency edge(s), 2 wave(s)
  wave 1: network
  wave 2: app
critical path (2 units): network -> app
peak concurrency: 1
```

Cette arête ne vient pas du code Terraform. Les deux modules ont des states
séparés qui ne partagent rien de lisible ; elle vient du `depends_on` déclaré
dans [`examples/terraform/argus.yaml`](../examples/terraform/argus.yaml).

```bash
argus deploy examples/terraform --backend terraform
```

Argus lance `terraform init` puis `terraform apply -auto-approve` dans
`network/`, attend, puis fait la même chose dans `app/`. Nettoyage :

```bash
cd examples/terraform/network && terraform destroy -auto-approve
cd ../app && terraform destroy -auto-approve
```

### Le cas mono-module (là où Argus ne sert pas)

```bash
argus graph examples/terraform/network --backend terraform
```

```
backend: terraform
1 unit(s), 0 dependency edge(s), 1 wave(s)
```

Une seule unité, aucune arête : **il n'y a rien à ordonner**. `terraform apply`
construit son propre graphe et parallélise ses trois ressources tout seul. Dans
le dashboard, ce cas affiche un bandeau qui le dit explicitement, et rend le
graphe interne obtenu par `terraform graph -type=plan` — c'est une
visualisation, pas une orchestration.

---

## Niveau 5 — LocalStack : de vrais déploiements CloudFormation

[LocalStack](https://localstack.cloud/) fait tourner une API AWS émulée dans
Docker. Argus s'y connecte via `--endpoint-url`, et le chemin de code exécuté
est **exactement le même** que contre AWS : mêmes appels boto3, même
`create_stack`, même polling `describe_stacks`.

Le dépôt utilise déjà LocalStack pour ses tests d'infrastructure — voir
[`infrastructure/scripts/README-tests-locaux.md`](../infrastructure/scripts/README-tests-locaux.md).
Ce niveau réutilise le même conteneur.

### Démarrer LocalStack

```bash
docker run -d --rm --name localstack \
  -p 4566:4566 \
  -e SERVICES=cloudformation,ec2,ecr,ecs,iam,logs,elasticloadbalancingv2,secretsmanager,cloudwatch,sns,s3 \
  localstack/localstack:3.8.1

# attendre qu'il réponde
until curl -sf http://localhost:4566/_localstack/health > /dev/null; do sleep 1; done
echo "LocalStack prêt"
```

### Des credentials factices

LocalStack ne les vérifie pas, mais boto3 refuse de construire une requête sans
eux :

```bash
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=eu-west-3
```

### Déployer

```bash
argus deploy infrastructure/cloudformation \
  --endpoint-url http://localhost:4566 \
  --region eu-west-3 \
  -p ProjectName=taskmanager -p Environment=dev
```

Argus affiche avant de demander confirmation :

```
target: LocalStack (region=eu-west-3, endpoint=http://localhost:4566)
```

Cette ligne est là pour une raison : c'est la garantie visuelle que vous n'êtes
pas en train de déployer sur un vrai compte. Argus considère comme « local »
tout endpoint qui ne se termine pas par `.amazonaws.com`.

Puis la pré-validation, puis le déploiement :

```
pre-flight: 12 template(s) validated
deploying 12 unit(s), strategy=rolling
Ctrl-C stops scheduling; units already started are left to finish.
  vpc                      IN_PROGRESS
  ecr                      IN_PROGRESS
  ...
```

### Dans le dashboard

Mettez `http://localhost:4566` dans le champ **Endpoint** en haut de page. La
page affiche alors :

> ⚡ Talking to http://localhost:4566, not to AWS. Nothing here touches a real account.

Décochez *Dry run*, tapez `cloudformation` pour confirmer, et regardez le graphe
s'allumer sur de vrais appels.

### Nettoyer

```bash
docker stop localstack
```

Tout disparaît avec le conteneur : LocalStack ne persiste rien par défaut.

### Ce que LocalStack ne reproduit pas fidèlement

* Les quotas, la latence réelle et les IAM policies ne sont pas simulés à
  l'identique. Un déploiement qui passe en local peut échouer sur un vrai compte
  pour une raison de permissions.
* La communauté (édition gratuite) ne couvre pas tous les services ni toutes les
  propriétés. Certaines des 12 stacks de ce dépôt peuvent échouer sur une
  propriété non supportée — c'est une limite de LocalStack, pas d'Argus, et
  c'est justement pour ça qu'Argus affiche la ressource fautive :

  ```
  FAILED codebuild: CodeBuildProject (AWS::CodeBuild::Project): <message de LocalStack>
  ```

> **Non vérifié ici.** Ce niveau 5 n'a **pas** pu être exécuté pendant l'écriture
> de ce guide : le démon Docker n'était pas démarré sur la machine de
> développement. Le chemin de code (`endpoint_url`, `create_stack`, polling) est
> couvert par des tests contre les modèles de service réels de botocore
> (`tests/test_aws_contract.py`), ce qui n'est pas la même chose que d'avoir
> tourné contre LocalStack. Si vous butez sur quelque chose ici, c'est une vraie
> possibilité — ouvrez une issue.

---

## Ce qui ne peut pas être fait en local

| Fonction | Pourquoi |
|---|---|
| `argus optimize` avec des métriques réelles | Il faut une charge réelle. CloudWatch existe dans LocalStack mais n'aura aucun datapoint : Argus répondra honnêtement `insufficient data`. |
| AWS Compute Optimizer | Non émulé par LocalStack. Argus le signale dans les `notes` et continue avec sa seule règle locale. |
| `argus explain` | Appelle l'API Anthropic : il faut `ANTHROPIC_API_KEY` (ou `ant auth login`). Il n'y a pas de mode hors ligne. |

Vous pouvez tout de même exercer le Module 3 sans AWS en lui donnant un rapport
écrit à la main :

```bash
argus optimize --service x --json > rapport.json   # ou fabriquez le JSON
argus explain --report rapport.json
```

---

## Argus en CI

Les niveaux 1 et 2 sont conçus pour tourner en CI sans aucun secret.

```yaml
- name: Argus — vérifier le graphe de dépendances
  run: |
    pip install -r requirements.txt
    # Échoue si le graphe a un cycle, un nom dupliqué, ou une déclaration
    # depends_on qui pointe dans le vide.
    python -m argus.cli graph infrastructure/cloudformation --format json > graph.json
    python -m argus.cli deploy infrastructure/cloudformation --dry-run --report-json run.json
```

Utile parce que ces commandes échouent (code de sortie non nul) sur :

* un cycle de dépendances introduit par un nouveau `Fn::ImportValue` ;
* deux templates portant le même nom de fichier ;
* un `depends_on` d'`argus.yaml` qui référence une unité supprimée.

Ce sont des erreurs qui, sans ça, ne se voient qu'au moment du déploiement.

> Le workflow [`ci.yml`](../.github/workflows/ci.yml) de ce dépôt **n'a pas** été
> modifié pour ajouter ce job — il appartient au pipeline d'origine.

---

## Dépannage

**`no backend given`** — passez `--backend cloudformation`, ou mettez
`backend:` dans `argus.yaml`. Argus cherche ce fichier depuis le chemin visé en
remontant jusqu'au dossier contenant `.git`.

**`no CloudFormation template with a Resources section found`** — le dossier ne
contient que des fichiers de paramètres. Argus ignore volontairement tout
fichier sans section `Resources`.

**`the dependency graph contains a cycle`** — le message nomme le cycle complet.
Deux stacks s'importent mutuellement ; il faut casser le cycle dans les
templates, aucun ordre n'existe.

**`two units share a name`** — deux fichiers de même nom de base (`vpc.yml` et
`vpc.yaml`). Ils s'écraseraient dans le graphe ; renommez-en un.

**`argus.yaml: unit 'x' depends on unknown unit 'y'`** — faute de frappe. C'est
volontairement une erreur et non un silence : une arête discrètement ignorée
donnerait un plan qui a l'air correct et qui ne l'est pas.

**Le dashboard ne démarre pas / port occupé** — `argus dashboard --port 8502`.

**`Port 4566 is already allocated`** — un LocalStack tourne déjà :
`docker stop localstack` puis relancez.
