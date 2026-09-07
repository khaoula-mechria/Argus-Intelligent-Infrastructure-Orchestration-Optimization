# Argus

**Argus déploie, visualise et optimise des infrastructures AWS décrites en
CloudFormation ou Terraform, en découvrant leurs dépendances réelles plutôt
qu'en suivant un ordre figé.**

---

## Sommaire

1. [Comment ça marche selon le backend](#comment-ça-marche-selon-le-backend)
2. [Installation](#installation)
3. [Quickstart CloudFormation](#quickstart-cloudformation)
4. [Quickstart Terraform](#quickstart-terraform)
5. [Convention d'input attendue](#convention-dinput-attendue)
6. [Le fichier `argus.yaml`](#le-fichier-argusyaml)
7. [Commandes disponibles](#commandes-disponibles)
8. [Les quatre modules](#les-quatre-modules)
9. [Limitations connues](#limitations-connues)
10. [Architecture du code](#architecture-du-code)
11. [Tests](#tests)

---

## Comment ça marche selon le backend

La valeur ajoutée d'Argus **n'est pas la même** selon le backend. Autant le dire
tout de suite plutôt que de vendre une symétrie qui n'existe pas.

### CloudFormation — c'est là qu'Argus sert le plus

CloudFormation n'offre aucun mécanisme natif pour déployer plusieurs stacks
indépendantes en parallèle en déduisant leur ordre depuis les
`Outputs.*.Export.Name` et les `Fn::ImportValue`. En pratique on maintient un
ordre à la main, dans un README ou dans un script, et cet ordre se désynchronise
des templates.

Argus lit les exports et les imports, en déduit le graphe, le découpe en vagues,
et déploie chaque vague en parallèle. **Le calcul de l'ordre et l'exécution
parallèle sont entièrement la contribution d'Argus.**

### Terraform — Argus fait moins, et le dit

`terraform apply` construit **déjà** nativement un graphe de dépendances et
parallélise les ressources indépendantes à l'intérieur d'un même state. Argus ne
réinvente pas ça et ne prétend pas faire mieux. Il en découle deux cas :

| Cas | Ce que fait Argus | Ce qu'il ne fait pas |
|---|---|---|
| **Un seul module racine** | Lit `terraform graph -type=plan` et le rend plus lisible que le DOT brut : nœuds nettoyés des nœuds de plomberie (providers, `root`, `(expand)`), arêtes réorientées dans le sens de la création. | **Aucune orchestration.** Terraform planifie ses ressources tout seul. Le dashboard affiche explicitement un bandeau qui le dit. |
| **Plusieurs modules racine** (states séparés) | Séquence les modules selon le `depends_on` déclaré dans `argus.yaml`, et laisse `terraform apply` paralléliser à l'intérieur de chacun. | Rien de magique : deux states séparés ne partagent aucune information lisible, donc l'ordre **doit** être déclaré. |

> **Honnêteté sur l'état de l'art.** [Terragrunt](https://terragrunt.gruntwork.io/)
> résout déjà le problème des dépendances entre modules racine, et le fait de
> façon plus mature qu'Argus. L'angle d'Argus n'est pas d'inventer quelque chose
> d'inédit là-dessus : c'est d'avoir **un seul graphe, un seul CLI et une seule
> UI** au-dessus des deux backends, plus les modules d'optimisation et
> d'explication.

---

## Installation

```bash
git clone https://github.com/khaoula-mechria/Argus-Intelligent-Infrastructure-Orchestration-Optimization.git
cd Argus-Intelligent-Infrastructure-Orchestration-Optimization

python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate

pip install -r requirements.txt
pip install -e .                 # facultatif : installe la commande `argus`
```

Sans `pip install -e .`, remplacez `argus` par `python -m argus.cli` dans tous
les exemples ci-dessous. Python 3.10 ou plus.

### Variables d'environnement

| Variable | Requise pour | Remarque |
|---|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (ou un profil / rôle) | `argus deploy` (CloudFormation), `argus optimize` | Résolues par boto3 selon la chaîne habituelle. |
| `AWS_REGION` | idem | Surchargeable par `--region` ou par `region:` dans `argus.yaml`. |
| `ANTHROPIC_API_KEY` | `argus explain` uniquement | Une session `ant auth login` fonctionne aussi. |

Aucune clé n'est nécessaire pour `argus graph`, ni pour `argus deploy --dry-run`,
ni pour le dashboard tant qu'on reste sur l'onglet Deployment en dry-run.

Le backend Terraform exige en plus le binaire `terraform` sur le `PATH` : Argus
délègue chaque `apply`, il ne le réimplémente pas.

---

## Quickstart CloudFormation

C'est le cas le mieux supporté. On l'exécute ici sur les 12 stacks de ce dépôt.

**1. Voir le graphe et les vagues, sans toucher à AWS :**

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
```

Les 12 étapes séquentielles documentées dans
[`infrastructure/README.md`](infrastructure/README.md) se ramènent à **7 vagues**,
et l'ordre calculé respecte les trois contraintes que ce document qualifie de
non négociables (`codebuild` avant `iam`, `secrets-manager` avant `codebuild` et
`ecs-task-definition`, `ecs-service` et `pipeline` avant `ecs-autoscaling`).
C'est vérifié par un test :
[`tests/test_cloudformation_adapter.py`](tests/test_cloudformation_adapter.py).

**2. Répéter le déroulé sans rien déployer :**

```bash
argus deploy infrastructure/cloudformation --dry-run
```

Le dry-run ne fait aucun appel cloud. Les durées affichées sont simulées et
étiquetées `SIMULATED` — elles montrent la structure en vagues, elles ne
prédisent pas un vrai déploiement.

**3. Déployer pour de vrai :**

```bash
argus deploy infrastructure/cloudformation --region eu-west-3
```

Argus affiche le plan puis demande une confirmation avant le premier appel AWS.
`--yes` la saute, `--plan-only` s'arrête avant.

**4. Autres formats de sortie :**

```bash
argus graph infrastructure/cloudformation --format dot   # Graphviz
argus graph infrastructure/cloudformation --format json  # pour outiller
```

---

## Quickstart Terraform

Le dépôt contient un projet exemple à deux modules racine, construit sur
`terraform_data` : **aucun provider, aucun compte cloud**, donc il est réellement
exécutable.

**1. Deux modules racine — Argus séquence :**

```bash
argus graph examples/terraform --backend terraform
```

```
backend: terraform
2 unit(s), 1 dependency edge(s), 2 wave(s)
  wave 1: network
  wave 2: app
```

Cette unique arête ne vient pas du code Terraform : elle vient du `depends_on`
déclaré dans [`examples/terraform/argus.yaml`](examples/terraform/argus.yaml).
Terraform ne peut pas la déduire, les deux states ne partagent rien.

```bash
argus deploy examples/terraform --backend terraform    # nécessite le binaire terraform
```

**2. Un seul module racine — Argus visualise seulement :**

```bash
argus graph examples/terraform/network --backend terraform
```

Ici Argus ne planifie rien : `terraform apply` ordonne et parallélise ses trois
ressources tout seul. Ce que le dashboard apporte, c'est le rendu du graphe
interne de Terraform (via `terraform graph -type=plan`), nettoyé de ses nœuds de
plomberie — et un bandeau qui dit noir sur blanc qu'Argus n'orchestre pas ici.

---

## Convention d'input attendue

Argus n'est pas magique. Il lui faut une convention minimale, la voici.

### CloudFormation

* Un dossier contenant les templates (`.yaml`, `.yml`, `.json`). Chaque fichier
  possédant une section `Resources` devient une **unité déployable** ; les autres
  fichiers sont ignorés.
* Le nom de la stack est le nom du fichier sans extension (préfixable par
  `stack_name_prefix`).
* **Les dépendances inter-stacks doivent passer par `Outputs.*.Export.Name` et
  `Fn::ImportValue`** — la convention AWS standard.

Argus normalise les deux côtés avec la même fonction, donc un nom d'export
contenant des paramètres non résolus (`${ProjectName}-${Environment}-vpc-id`)
s'apparie quand même : **aucun appel AWS n'est nécessaire pour construire le
graphe**. Passer les vraies valeurs avec `-p ProjectName=taskmanager` remplace
juste les placeholders par les noms d'export réels.

> **Si votre projet ne suit pas cette convention** — valeurs passées en
> paramètres de stack, ou codées en dur — Argus **ne peut pas** déduire l'ordre.
> Il ne tente aucun fallback fragile : il vous laisse le déclarer explicitement
> sous `depends_on`. Voir [Limitations connues](#limitations-connues), qui décrit
> le cas réel présent dans ce dépôt.

### Terraform

* **Un seul module racine** : pointez Argus sur le dossier. Aucun `argus.yaml`
  requis. Argus visualise, il n'orchestre pas.
* **Plusieurs modules racine** (chacun son state) : déclarez-les sous `modules:`
  dans un `argus.yaml`, avec leurs `depends_on`. C'est **obligatoire** — sans ça
  Argus n'a aucune information sur laquelle se baser.

---

## Le fichier `argus.yaml`

Optionnel pour CloudFormation, requis pour un Terraform multi-modules. Argus le
cherche à partir du chemin visé en remontant jusqu'à la racine du dépôt (le
dossier qui contient `.git`).

```yaml
backend: cloudformation          # cloudformation | terraform
path: infrastructure/cloudformation
region: eu-west-3
max_parallel: 6                  # plafond d'unités déployées simultanément
stack_name_prefix: ""            # préfixe des noms de stacks CloudFormation

parameters:                      # passés aux stacks qui les déclarent
  ProjectName: taskmanager       # (filtrés par template : pas de ValidationError)
  Environment: dev

# Arêtes qu'Argus ne peut PAS déduire de la source.
depends_on:
  alb:
    - vpc

# Terraform uniquement : les modules racine du projet.
modules:
  - name: network
    path: ./network
  - name: app
    path: ./app
    depends_on: [network]
    variables:
      environment: dev
    var_files: [prod.tfvars]
```

Une faute de frappe dans `depends_on` ou dans un `modules[].depends_on` est une
**erreur**, pas un silence : une arête discrètement ignorée est exactement le
mode de défaillance qu'un outil d'ordonnancement ne peut pas se permettre.

Le fichier de ce dépôt est [`argus.yaml`](argus.yaml).

---

## Commandes disponibles

```
argus graph PATH        # affiche le graphe et les vagues (text | dot | json)
argus deploy PATH       # ordonne et déploie, vague par vague
argus optimize          # analyse le dimensionnement d'une ressource
argus explain           # explique un rapport d'optimisation en langage naturel
argus dashboard         # lance le front Streamlit
```

### `argus deploy`

```bash
argus deploy infrastructure/cloudformation \
  --backend cloudformation \
  -p ProjectName=taskmanager -p Environment=dev \
  --region eu-west-3 \
  --max-parallel 6
```

| Option | Effet |
|---|---|
| `--backend` | `cloudformation` ou `terraform`. Sinon lu dans `argus.yaml`. |
| `--config` | Chemin explicite vers un `argus.yaml`. |
| `-p / --parameter` | `Clé=Valeur`, répétable. Prioritaire sur `argus.yaml`. |
| `--plan-only` | Affiche le plan et s'arrête. |
| `--dry-run` | Déroule le plan avec des déploiements simulés. Aucun appel cloud. |
| `--yes` | Saute la confirmation avant un déploiement réel. |
| `--max-parallel` | Plafond d'unités simultanées dans une vague. |

À la fin, Argus mesure l'horloge murale et la compare à la somme des durées
individuelles — ce qu'un déroulé strictement séquentiel aurait coûté. Voici la
sortie réelle du dry-run sur les 12 stacks de ce dépôt :

```
SIMULATED wall clock (parallel waves) : 1.5s
SIMULATED sequential equivalent       : 2.0s
SIMULATED saved                       : 0.5s (x1.30)
```

Le préfixe `SIMULATED` n'apparaît qu'en dry-run, et ces durées-là sont dérivées
du nombre de ressources par template, pas d'AWS. Sur un vrai déploiement le même
bloc s'affiche sans préfixe, avec des mesures réelles — et l'écart y est bien
plus marqué, puisque les stacks lentes (VPC, ALB) durent des minutes et non des
millisecondes.

Une unité dont une dépendance a échoué est marquée `SKIPPED` et n'est pas tentée :
elle échouerait de toute façon sur un export manquant et ajouterait une seconde
erreur trompeuse au rapport.

### `argus optimize`

```bash
argus optimize --service taskmanager-dev-service --region eu-west-3
argus optimize --resource i-0123456789abcdef0 --type ec2-instance
argus optimize --service taskmanager-dev-service --json > rapport.json
```

Types supportés : `ecs-service`, `ec2-instance`, `rds-instance`, `ebs-volume`.
Le cluster ECS est découvert automatiquement si `--cluster` est omis.

### `argus explain`

```bash
argus explain --service taskmanager-dev-service
argus explain --report rapport.json --ask "512 CPU suffit pour un pic ?"
argus explain --report rapport.json --apply
```

`--apply` **n'applique rien**. Il affiche les commandes qu'un humain devrait
lancer. Argus n'a aucun chemin de code qui redimensionne une ressource.

### `argus dashboard`

```bash
argus dashboard --port 8501
```

---

## Les quatre modules

### Module 1 — Orchestrateur

`discover_units` → `build_dependency_graph` → tri topologique en vagues →
déploiement parallèle intra-vague → mesure parallèle vs séquentiel. Écrit
entièrement au-dessus de l'interface `InfrastructureAdapter` : il ne sait pas ce
qu'est une stack ni un state.

### Module 2 — Optimization Engine

Lit les métriques CloudWatch d'une ressource sur une fenêtre glissante (14 jours
par défaut), applique **une règle de seuil explicite**, et affiche son verdict
**à côté** de celui d'AWS Compute Optimizer.

La règle : si le p95 de toutes les métriques d'utilisation est sous **40 %**, la
ressource est surdimensionnée et descend d'un cran ; si une métrique dépasse
**80 %**, elle monte d'un cran ; sinon rien ne bouge. Elle ne se déplace que le
long d'échelles que la plateforme accepte réellement — les paires CPU/mémoire
Fargate documentées, les suffixes de taille d'une famille EC2 — et si la taille
actuelle n'est pas sur l'échelle, elle renvoie un constat **sans cible** plutôt
qu'une combinaison qu'ECS refuserait.

Les deux verdicts sont juxtaposés volontairement : Compute Optimizer exige ~14
jours d'historique et reste muet en dessous, la règle locale produit toujours
quelque chose. **Leur désaccord est une information, pas un bug** — et il est
affiché comme tel.

Les volumes EBS sont lus mais délibérément non dimensionnés par cette règle :
leur question de rightsizing porte sur les IOPS et le débit, pas sur des
pourcentages d'utilisation.

### Module 3 — Couche d'explication

Passe le rapport du Module 2 à Claude (`claude-opus-5` par défaut) avec un
system prompt qui exige que **chaque affirmation soit ancrée dans un chiffre du
rapport**, et qui impose de dire platement quand les données sont trop maigres
plutôt que de combler le vide avec des généralités.

Aucune auto-application. `--apply` imprime des commandes ; c'est un humain qui
les lit et les lance, et c'est aussi là qu'une mauvaise recommandation se fait
attraper.

### Module 4 — Front Streamlit

Trois onglets (Deployment / Optimization / Explanation), un sélecteur de backend
et un chemin de projet en haut de page. Comme tous les adaptateurs renvoient le
même `networkx.DiGraph`, **aucun code de rendu ne branche sur le backend**.

* Nœuds colorés par statut, arêtes vers une unité `IN_PROGRESS` épaissies.
* Une arête déclarée dans `argus.yaml` est dessinée en pointillés et étiquetée :
  on distingue ce qu'Argus a découvert de ce qu'un humain a affirmé.
* Chrono comparatif séquentiel vs parallèle.
* Le déploiement est en dry-run par défaut ; un déploiement réel exige de taper
  le nom du backend pour confirmer, parce que la page ne peut pas l'annuler.
* Le seul élément spécifique au backend est le bandeau qui dit ce qu'Argus fait
  réellement — voir [Comment ça marche selon le backend](#comment-ça-marche-selon-le-backend).

---

## Limitations connues

**1. CloudFormation sans exports/imports.** Si une stack reçoit la valeur d'une
autre en **paramètre** plutôt que via `Fn::ImportValue`, Argus ne voit pas la
dépendance. Aucun fallback n'est tenté : deviner produirait un ordre plausible et
faux.

> **Ce dépôt en contient un cas réel.** `alb.yaml` déclare `VpcId` et
> `PublicSubnetIds` en paramètres (choix assumé du template, dont les
> descriptions désignent `vpc.yaml` comme source attendue). Aucune paire
> export/import n'existe, donc Argus placerait l'ALB dans la même vague que le
> VPC. L'arête est déclarée sous `depends_on` dans [`argus.yaml`](argus.yaml) —
> et c'est pour ça qu'il y a 21 arêtes et non 20.

**2. Imports non satisfaits.** Un `Fn::ImportValue` que rien dans le projet
n'exporte est signalé (`warning` en CLI, encadré dans l'UI) et non ordonné. Il
peut être légitime — un export produit hors du projet — mais Argus ne peut pas le
savoir, alors il le dit au lieu de l'ignorer.

**3. Terraform mono-module : aucune orchestration.** Voir plus haut. Argus
visualise, Terraform ordonne.

**4. Terraform multi-modules sans `argus.yaml`.** Sans `modules:` déclarés,
Argus traite le dossier comme un module racine unique. Il ne va pas deviner une
dépendance entre deux states.

**5. Pas d'auto-remédiation.** Aucun module ne modifie de ressource. Le Module 3
imprime des commandes, il ne les exécute pas.

**6. Templates de plus de 51 200 octets.** `CreateStack` refuse un corps inline
au-delà ; il faut passer par S3 avec `TemplateURL`. Argus ne gère pas ce bucket
et le dit explicitement dans l'erreur. (Les 12 templates de ce dépôt sont tous en
dessous.)

**7. `get_unit_status` sur Terraform** répond d'après le state (des ressources
présentes ou non). Il ne distingue pas un module appliqué d'un module qui a
dérivé depuis.

**8. Ce qui n'a pas été exécuté ici.** Le graphe, l'ordonnancement, le parsing
DOT, le front et le dry-run tournent et sont testés dans ce dépôt. En revanche,
faute de credentials AWS valides et de binaire `terraform` dans l'environnement
de développement, **le chemin `deploy` réel (boto3 `create_stack`, `terraform
apply`) et les appels CloudWatch / Compute Optimizer n'ont pas été exercés contre
de vraies API** — ils sont couverts par des tests à clients simulés, ce qui n'est
pas la même chose.

---

## Architecture du code

```
argus/
  adapters/
    base.py            InfrastructureAdapter, DeployableUnit, UnitStatus
    cloudformation.py  exports/imports -> graphe, déploiement via boto3
    terraform.py       terraform graph -> DOT, modules racine, apply délégué
  orchestrator.py      Module 1 : vagues, parallélisme, mesure du gain
  optimizer.py         Module 2 : CloudWatch + règle de seuil + Compute Optimizer
  explainer.py         Module 3 : API Anthropic, jamais d'auto-application
  dashboard.py         Module 4 : Streamlit, agnostique du backend
  config.py            lecture d'argus.yaml
  cli.py               la commande `argus`
```

L'interface commune :

```python
class InfrastructureAdapter(ABC):
    backend: str

    def discover_units(self, path) -> list[DeployableUnit]: ...
    def build_dependency_graph(self, units) -> nx.DiGraph: ...   # implémentée par défaut
    def deploy_unit(self, unit) -> DeploymentResult: ...
    def get_unit_status(self, unit) -> UnitStatus: ...
    def inspect_unit(self, unit) -> nx.DiGraph | None: ...       # optionnelle, affichage seul
```

`build_dependency_graph` a une implémentation par défaut qui apparie `provides`
contre `requires` — elle couvre les exports/imports CloudFormation **et** les
`depends_on` de modules Terraform, donc les deux backends partagent ce code.

Ajouter un troisième backend = ajouter un fichier dans `argus/adapters/`. Rien
au-dessus de cette couche ne change.

---

## Tests

```bash
python -m pytest tests -q
```

113 tests, sans credentials AWS ni binaire `terraform` : les appels cloud passent
par des clients simulés, `terraform` par une couture (`runner`), et le parsing
DOT est vérifié contre une sortie réelle de `terraform graph -type=plan`
capturée dans [`tests/fixtures/`](tests/fixtures/).

Une partie des tests s'exécute contre les **vraies stacks de ce dépôt** : le
graphe est acyclique, aucun import n'est laissé insatisfait, et l'ordre calculé
respecte les contraintes documentées. Le front est vérifié via
`streamlit.testing` — la page se charge sans exception, affiche le plan réel à 12
unités / 7 vagues, et un déploiement en dry-run va jusqu'au bout.

---

## Le pipeline CI/CD d'origine

Ce dépôt est parti d'un projet de pipeline CI/CD complet (CodePipeline, CodeBuild,
ECR, ECS Fargate, CodeDeploy Blue/Green). Cette partie n'a **pas** été modifiée :
Argus ajoute une couche d'abstraction au-dessus des templates, il n'y touche pas.

Sa documentation est conservée dans
[`docs/pipeline-cicd.md`](docs/pipeline-cicd.md), et le détail de
l'infrastructure dans [`infrastructure/README.md`](infrastructure/README.md).
