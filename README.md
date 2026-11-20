# Pairi Daiza — alerte du 29 décembre 2026

Ce projet teste si la date du **29/12/2026**, pour **1 nuit et 2 adultes**, n'est plus grisée sur le calendrier de réservation Pairi Daiza.

⚠️ **Prototype à valider** : le calendrier est dynamique et son HTML n'a pas pu être inspecté dans l'environnement de préparation. Le script refuse d'envoyer une alerte quand son interprétation est incertaine. Avant de laisser fonctionner la surveillance, téléchargez la capture de diagnostic, vérifiez la date et l'état, et ajustez la détection si nécessaire.

## Installation

1. Créer un dépôt GitHub **public** vide, par exemple `pairidaiza-alerte`. Les dépôts publics offrent des minutes gratuites pour les runners standard GitHub Actions, sous réserve des politiques d'usage de GitHub.
2. Importer le contenu de ce dossier via **Add file > Upload files** en conservant le chemin `.github/workflows/monitor.yml`. Les fichiers cachés peuvent nécessiter la création du workflow via **Add file > Create new file**, en écrivant son chemin complet.
3. Ouvrir **Actions > Surveiller Pairi Daiza > Run workflow**. Laisser `diagnostic` coché.
4. Lorsque le test est fini, ouvrir son exécution > **Artifacts > diagnostic-...**. Examiner `calendar.png` et `result.json`.
5. Le résultat doit afficher décembre 2026 et le 29 grisé (`status: unavailable`). Sinon, le script doit être adapté au calendrier réel. **Ne pas activer les alertes avant cette validation.**
6. Dans **Settings > Secrets and variables > Actions > New repository secret**, ajouter :
   - `SMTP_USER` : adresse Gmail de l'expéditeur
   - `SMTP_APP_PASSWORD` : mot de passe d'application Gmail (compte compatible, validation en 2 étapes activée)
   - `EMAIL_TO` : adresse qui reçoit les alertes
7. Dans **Settings > Actions > General > Workflow permissions**, vérifier que les autorisations sont compatibles avec `issues: write` (ce workflow conserve son état dans une issue GitHub visible publiquement).
8. Pour un test complet, exécuter **Run workflow** en décochant `diagnostic`, après avoir validé la détection.
9. **Activer seulement après validation** : Settings > Secrets and variables > Actions > Variables > New repository variable : nom `ENABLE_MONITOR`, valeur `true`. Sans cette variable, les contrôles planifiés sont ignorés.
10. Les contrôles planifiés sont à :07, :22, :37 et :52 UTC de chaque heure, avec possibilité de retards GitHub. Le robot ne doit pas être considéré comme une alerte temps réel garantie.

## Fonctionnement

- Date actuellement indisponible : aucune notification.
- Premier passage à disponible : un e-mail est envoyé, puis l'état est mémorisé dans une issue GitHub publique.
- Toujours disponible : pas d'e-mail répété.
- Redevient indisponible puis disponible : nouvelle alerte.
- État non interprétable : exécution en échec, aucune fausse alerte volontaire.

La surveillance programmée continue tant qu'elle n'est pas désactivée. **Désactiver manuellement le workflow** après réservation / après le 29 décembre 2026.

## Limites connues

- Le robot ne choisit pas tous les types de chambres ou animaux : il lit uniquement la disponibilité visuelle de la date pour la durée 1 nuit. Vérifier que le calendrier affiché sous Playwright correspond bien à la recherche souhaitée.
- Si décembre 2026 ne s'affiche pas, le robot recherche un bouton « next » accessible. Certains calendriers nécessiteront un sélecteur spécifique ou des clics supplémentaires.
- Le résultat « available » signifie que la date est colorée selon le calendrier : vérifier les chambres réellement disponibles lors de la réservation.
- L'issue d'état `monitor-state` est publique et ne contient aucune adresse e-mail.
- Les actions planifiées peuvent être retardées ou désactivées après 60 jours d'inactivité d'un dépôt public.
- Utiliser une fréquence raisonnable, respecter les conditions du site et ne pas contourner les contrôles d'accès.
