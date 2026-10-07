# Thuisbord for Home Assistant

[Nederlands](#nederlands) · [English](#english)

Thuisbord turns a screen in your home into a shared household screen for energy and family. This integration sends your smart meter's readings from Home Assistant to your household's Thuisbord. It is a bridge only: it reads the sensors you choose and controls no devices.

---

## Nederlands

### Wat het doet

- Je koppelt Home Assistant aan jullie Thuisbord met de **koppelsleutel** uit de Thuisbord-app. Home Assistant controleert de sleutel meteen bij Thuisbord.
- Je kiest jullie sensoren: vermogen (nodig), en als jullie meter ze geeft de meterstanden per tarief of totaal, het actieve tarief, zonnestroom en gas. De integratie stuurt dan zelf een meting, hooguit elke 10 seconden.
- Of je kiest voor de actie **Thuisbord: Meting sturen** en maakt zelf een automatisering.

### Installeren via HACS

1. Open HACS in Home Assistant.
2. Kies rechtsboven ⋮ › **Aangepaste repositories**.
3. Plak `https://github.com/mennovanhout/thuisbord-home-assistant`, kies het type **Integratie** en klik op **Toevoegen**.
4. Zoek **Thuisbord** in HACS, klik op **Downloaden** en herstart Home Assistant.
5. Ga naar **Instellingen › Apparaten en diensten › Integratie toevoegen** en kies **Thuisbord**.
6. Kopieer in de Thuisbord-app de sleutel bij **Instellingen › Meterkoppeling › Meter koppelen › Home Assistant** en plak hem in het venster.
7. Kies **Ik kies mijn sensoren** en kies jullie vermogenssensor. Klaar.

Werkt Thuisbord niet meer met de sleutel, bijvoorbeeld omdat iemand in de app een nieuwe heeft gemaakt? Dan vraagt Home Assistant om de nieuwe sleutel. De sensor **Thuisbord Status** laat zien wat de integratie doet.

### Wat Thuisbord krijgt

Alleen de waarden die je kiest, hooguit één meting per 10 seconden: vermogen, meterstanden en tarief, zonnestroom en gas. Niets anders uit Home Assistant. De sleutel blijft in deze integratie, staat nooit in het logboek en wordt in diagnostische gegevens weggelaten. Meetwaarden komen ook niet in het logboek.

---

## English

### What it does

- You connect Home Assistant to your Thuisbord with the **connection key** from the Thuisbord app. Home Assistant checks the key with Thuisbord at once.
- You choose your sensors: power (required) and, when your meter reports them, totals per tariff or combined, the active tariff, solar and gas. The integration then sends a reading by itself, at most every 10 seconds.
- Or you choose the action **Thuisbord: Send reading** and build your own automation.

### Install through HACS

1. Open HACS in Home Assistant.
2. Choose ⋮ › **Custom repositories** in the top right.
3. Paste `https://github.com/mennovanhout/thuisbord-home-assistant`, choose the type **Integration** and click **Add**.
4. Find **Thuisbord** in HACS, click **Download** and restart Home Assistant.
5. Go to **Settings › Devices & services › Add integration** and choose **Thuisbord**.
6. In the Thuisbord app, copy the key under **Settings › Meter connection › Connect meter › Home Assistant** and paste it in the window.
7. Choose **I choose my sensors** and pick your power sensor. Done.

If Thuisbord stops accepting the key, for example because someone made a new one in the app, Home Assistant asks for the new key. The **Thuisbord Status** sensor shows what the integration is doing.

### What Thuisbord receives

Only the values you choose, at most one reading every 10 seconds: power, meter totals and tariff, solar and gas. Nothing else from Home Assistant. The key stays in this integration, never reaches the log and is redacted from diagnostics. Readings never reach the log either.

### The action

```yaml
action: thuisbord.send_reading
data:
  config_entry_id: <your Thuisbord entry>
  active_power_w: "{{ states('sensor.p1_meter_power') }}"
  import_t1_kwh: "{{ states('sensor.p1_meter_energy_import_tariff_1') }}"
  import_t2_kwh: "{{ states('sensor.p1_meter_energy_import_tariff_2') }}"
```

Only `active_power_w` is required. Totals per tariff go in pairs. A reading less than 10 seconds after the previous one is skipped; with `response_variable` the action answers `queued`, `too_soon` or an error.

---

## Development

Requires Home Assistant 2026.3 or newer. No Python requirements beyond Home Assistant itself.

Tests use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component), which needs Linux and Python 3.14:

```sh
pip install -r requirements_test.txt
python -m pytest
```

On Windows, run them in a container:

```sh
docker run --rm -v "$PWD:/src" -w /src python:3.14-slim sh -c "pip install -q -r requirements_test.txt && python -m pytest -q"
```

CI runs the tests, [hassfest](https://github.com/home-assistant/actions) and the [HACS action](https://hacs.xyz/docs/publish/action).

The API this integration calls is Thuisbord's: `GET /connection` to check a key and `POST /readings` to send readings, with the connection key as a bearer token.

### Documentation used

- Home Assistant developer docs: [integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest), [config flow](https://developers.home-assistant.io/docs/core/integration/config_flow), [options flow](https://developers.home-assistant.io/docs/config_entries_options_flow_handler), [config entries](https://developers.home-assistant.io/docs/config_entries_index), [service actions](https://developers.home-assistant.io/docs/dev_101_services), [raising exceptions](https://developers.home-assistant.io/docs/core/platform/raising_exceptions), [custom integration translations](https://developers.home-assistant.io/docs/internationalization/custom_integration), [diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics), [inject-websession](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/inject-websession), [listening to events](https://developers.home-assistant.io/docs/integration_listen_events), [brand images](https://developers.home-assistant.io/docs/core/integration/brand_images), [repairs](https://developers.home-assistant.io/docs/core/platform/repairs)
- Home Assistant user docs: [selectors](https://www.home-assistant.io/docs/blueprint/selectors/)
- HACS: [publishing](https://hacs.xyz/docs/publish/start), [integrations](https://hacs.xyz/docs/publish/integration), [inclusion](https://hacs.xyz/docs/publish/include), [action](https://hacs.xyz/docs/publish/action)

## Licence

MIT, see [LICENSE](LICENSE).
