# Contributor checks

- Run `python3 -m pytest -q test_munbyn_print.py`; PDF checks require `pdftoppm`.
- Use a static system account for BLE services: DynamicUser failed system D-Bus authentication on the tested Debian host.
- Connect by the intended printer's exact MAC, because multiple devices can advertise the same model name.
