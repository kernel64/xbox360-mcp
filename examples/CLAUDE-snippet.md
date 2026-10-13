## Xbox 360 (serveur MCP `xbox360`)

La console RGH est accessible via les outils `xbox_*`.

- Sortie de build : `build\Release\` (contient `default.xex`)  <!-- adapter -->
- Dossier console : `Hdd:\Devkit\Harissa64`                       <!-- adapter -->
- Log affiché quand l'init est OK : `init ok`                    <!-- adapter : regex -->

Boucle de test après chaque modification :
1. Compiler.
2. `xbox_deploy(local_dir=<sortie>, remote_dir=<dossier console>, launch=true, wait_for=<log init>)`.
3. Lire la suite avec `xbox_debug_log(since_id=<next_since_id précédent>)`.
4. Si `timed_out` ou ligne `[exception]` : `xbox_crash_report`, analyser PC/LR/backtrace vs le code source
   (module `default.xex`, base 0x82000000 en général ; utiliser le .map/.pdb du build pour retrouver la fonction).
5. `xbox_screenshot` pour vérifier le rendu.
6. Après un crash : `xbox_reboot` avant de relancer.

Logger avec `OutputDebugStringA` / `DbgPrint` côté homebrew : c'est ce que remonte `xbox_debug_log`.
