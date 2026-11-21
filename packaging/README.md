# Установка компонентов LOCALAI

## Служба чтения документов (C-401)

`install-vision.ps1` устанавливает дополнительную автозапускаемую службу `LocalAIVision` на Windows x64. Подготовьте Python 3.13 x64 с доступом для служб, WinSW 2.12.0 x64, два файла моделей Tesseract и каталог колёс под эту версию Python и Windows. Сервер LOCALAI должен быть настроен отдельно. [WinSW 2.12.0](https://github.com/winsw/winsw/releases/tag/v2.12.0), [настройка служебной роли и моделей](../vision/README.md).

На машине с интернетом комплект колёс можно собрать командой:

```powershell
py -3.13 -m pip download --only-binary=:all: --platform win_amd64 --python-version 313 --implementation cp --abi cp313 -r vision/requirements.txt -d bundle/wheels
```

Скопируйте `packaging`, `vision` и подготовленный комплект на станцию. Из PowerShell администратора запустите установку, указав действующий адрес сервера и имя служебной учётной записи LOCALAI:

```powershell
$worker = Get-Credential -UserName 'vision-worker' -Message 'Учётная запись очереди LOCALAI'
.\packaging\install-vision.ps1 -Python 'C:\Python313\python.exe' -WinSW '.\bundle\WinSW-x64.exe' -Wheelhouse '.\bundle\wheels' -Tessdata '.\bundle\tessdata' -ServerUrl 'http://127.0.0.1:8765' -Worker $worker
Get-Service LocalAIVision
```

Для своего сертификата HTTPS укажите `-CaFile`. Скрипт ставит зависимости из локального комплекта в отдельное окружение, копирует код и модели в `%ProgramFiles%\LOCALAI\Vision`. Служба запускается от LocalService. В `%ProgramData%\LOCALAI\Vision` пароль доступен администраторам, системе и службе через её SID. Запись службе разрешена в подкаталоги `logs` и `cache`. Учётные данные LOCALAI используются для входа в HTTP API.

На Windows проверьте автозапуск после перезагрузки, загрузите PDF и дождитесь состояния «Прочитан» (`ready`) в API. Для удаления службы остановите её через `Stop-Service LocalAIVision` и вызовите `LocalAIVision.exe uninstall` из каталога компонента. Старые данные и конфигурацию удаляет администратор.
