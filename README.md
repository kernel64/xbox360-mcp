# xbox360-mcp

J'en avais marre de faire des allers-retours entre le PC et la Xbox à chaque modif d'un homebrew :
copier le xex en FTP, le lancer depuis Aurora, regarder la télé, deviner pourquoi ça plante...
Du coup j'ai écrit ce petit outil qui parle directement à xbdm (le debug monitor de la 360, port 730)
et qui s'utilise de trois façons :

- comme serveur MCP, pour que Claude Code puisse déployer, lancer, lire les logs, faire des
  screenshots et analyser les crashs tout seul ;
- en ligne de commande (`xbox360 ...`), pour les scripts de build ;
- comme petite librairie Python.

Ça marche sur une console RGH/JTAG avec xbdm chargé. Je l'utilise sur une Jtag Trinity
(kernel 17559, Aurora 0.7b).

## Côté console

Il faut que `xbdm.xex` soit chargé par DashLaunch. Dans `launch.ini` :

```ini
[Plugins]
plugin1 = Hdd:\xbdm.xex
plugin2 = Hdd:\JRPC2.xex   ; facultatif
```

JRPC2 n'est pas obligatoire, il sert juste pour les températures, le title ID et les
notifications à l'écran (XNotify). Sans lui, tout le reste fonctionne.

La console et le PC doivent être sur le même réseau. Pour vérifier que xbdm répond :

```powershell
Test-NetConnection 192.168.1.50 -Port 730
```

(remplace l'IP par celle de ta console, Aurora l'affiche en bas de l'écran). Toutes les connexions
partent du PC, donc pas besoin d'ouvrir quoi que ce soit dans le pare-feu.

## Installation

J'utilise [uv](https://docs.astral.sh/uv/) :

```powershell
git clone https://github.com/kernel64/xbox360-mcp
cd xbox360-mcp
uv sync
$env:XBOX360_HOST = "192.168.1.50"
uv run xbox360 info
```

Si `info` te renvoie le nom de la console et le titre en cours, c'est bon.
Sans uv, un `pip install -e .` dans un venv fait l'affaire.

## Avec Claude Code

```powershell
claude mcp add xbox360 --scope user -e XBOX360_HOST=192.168.1.50 -- uv --directory C:\chemin\vers\xbox360-mcp run xbox360-mcp
```

Tu peux aussi le déclarer par projet avec un `.mcp.json` (il y a un modèle dans
`examples/mcp.example.json`). Ensuite `/mcp` dans Claude Code doit afficher `xbox360` connecté.

Dans `examples/CLAUDE-snippet.md` il y a un bout de texte à coller dans le `CLAUDE.md` de ton
projet homebrew : il explique à Claude quel dossier déployer et quel message de log attendre au
démarrage. Avec ça, tu peux lui demander des choses comme « compile en Release, déploie, lance, attends
`init ok` et si ça plante montre-moi le crash report », et il se débrouille.

Ce qu'il a à disposition :

- **console** : état, lecteurs, reboot, notification à l'écran, commande xbdm brute ;
- **fichiers** : lister, télécharger, lire un fichier texte, envoyer, créer, renommer, supprimer ;
- **dev** : `xbox_deploy` envoie seulement les fichiers modifiés, puis peut lancer le xex et attendre
  une ligne de log précise ; `xbox_launch` lance un xex déjà présent ;
- **logs** : tout ce que le titre envoie via `OutputDebugString`/`DbgPrint`, plus les exceptions,
  chargements de modules, etc., capturés en continu en arrière-plan ;
- **écran** : screenshot du framebuffer, renvoyé directement en image ;
- **debug** : crash report (threads arrêtés, registres, PC/LR résolus en `module+offset`, backtrace,
  désassemblage), threads, breakpoints, stop/go ;
- **mémoire** : lecture, écriture, recherche de motifs avec jokers, désassemblage PowerPC (capstone),
  carte mémoire, modules.

## Configuration

Tout passe par des variables d'environnement :

| Variable | Défaut | |
|---|---|---|
| `XBOX360_HOST` | | IP de la console (obligatoire) |
| `XBOX360_PORT` | `730` | |
| `XBOX360_TIMEOUT` | `10` | en secondes |
| `XBOX360_DEPLOY_ROOT` | `Hdd:\Devkit` | où `xbox_deploy` envoie les builds par défaut |
| `XBOX360_LOG_FILE` | | si défini, tous les logs sont aussi écrits dans ce fichier |
| `XBOX360_READ_ONLY` | `0` | voir plus bas |
| `XBOX360_ATTACH_DEBUGGER` | `1` | voir plus bas |

**Mode lecture seule.** Avec `XBOX360_READ_ONLY=1`, rien ne peut être écrit, supprimé, lancé ou
redémarré, et la commande brute n'accepte que des commandes de lecture connues. Je conseille de
commencer comme ça pour tester.

**Debugger.** Par défaut l'outil s'attache comme debugger. L'intérêt, c'est qu'un crash fige le jeu
au lieu de revenir au dashboard, et qu'on peut donc aller voir ce qui s'est passé. En lecture seule
il ne s'attache pas, sauf si tu forces `XBOX360_ATTACH_DEBUGGER=1`.

**Dossiers protégés.** Même hors lecture seule, la suppression et le renommage refusent les racines
des lecteurs, `Flash:`, `Hdd:\Content` (profils, DLC, mises à jour) et `Hdd:\Cache`. Si tu sais ce
que tu fais, la CLI a un `rm --force`.

## En ligne de commande

```powershell
uv run xbox360 info
uv run xbox360 ls Hdd:\Apps
uv run xbox360 deploy .\build\Release Hdd:\Devkit\MonJeu --run --wait "init ok"
uv run xbox360 log -f
uv run xbox360 screenshot capture.png
uv run xbox360 mem 82000000 100 --format u32
uv run xbox360 dis 82001234 20
uv run xbox360 crash
uv run xbox360 raw dmversion
```

Pour `mem`, l'adresse et la longueur sont en hexa (`100` = 256 octets). `deploy --prune` supprime
aussi sur la console les fichiers que tu as retirés en local. Attention, ça inclut ce que le jeu a pu
écrire lui-même dans ce dossier (sauvegardes, configs).

## En Python

```python
from xbox360_mcp import XboxConsole

x = XboxConsole("192.168.1.50")
print(x.info())
x.upload_file("Hdd:\\Devkit\\test.bin", "test.bin")
data, illisible = x.read_memory(0x82000000, 0x100)
```

## Tests

`tests/mock_xbdm.py` est une fausse console qui imite xbdm (fichiers, mémoire, threads, logs,
screenshot, JRPC2). Les tests tournent contre elle :

```powershell
uv run --extra dev pytest
```

Tu peux aussi la lancer à part (`python tests/mock_xbdm.py --port 7300`) et pointer l'outil dessus
avec `XBOX360_HOST=127.0.0.1` et `XBOX360_PORT=7300` pour essayer sans Xbox.

## Quelques remarques

- Les screenshots sont en BGRA sur une vraie console. Si un jour les couleurs sont bizarres,
  l'option `channel_order` (ou `--order` en CLI) permet d'essayer un autre ordre.
- Sans JRPC2, xbdm répond `DEBUGGING` à toutes les commandes `consolefeatures`. L'outil le détecte et
  considère simplement que le plugin n'est pas là.
- xbdm n'accepte que 4 connexions à peu près en même temps. Si tu as une erreur `401`, ferme les
  autres outils (Neighborhood, etc.).
- Pas encore de breakpoints mémoire ni de pas-à-pas dédiés. En attendant, ça passe par la commande
  brute (`break read=0x... size=4`, `step`...).
- Le format de certaines réponses (`getcontext` notamment) peut varier selon la version de xbdm. Si
  quelque chose manque, la commande brute montre la réponse telle quelle.
