- **Delivery client moved above the integrations leaf (layering fix)** ✅ — the
  native delivery client lived in `prefrontal/integrations/delivery.py` yet imported
  *up* into `prefrontal.webhooks.notify` (eagerly) and `prefrontal.coaching` (six
  lazy imports), even though `integrations/` is documented as a low-level leaf that
  low-level modules import (e.g. `todos` → `OllamaError`). The cycle was held off
  only by a fragile convention (not re-exporting `delivery` from the package) plus
  the lazy imports. It now lives at **`prefrontal/delivery.py`**, one layer up, where
  depending on `coaching` and the notification button builder is legitimate: the
  six lazy `coaching` imports and the `TYPE_CHECKING` cycle-dodge collapse into a
  single top-level import, and `integrations/` is a true transport leaf again (APNs
  / Twilio / SMTP wire clients only). Import path changes from
  `prefrontal.integrations.delivery` to `prefrontal.delivery`; all call sites,
  tests, and the `integrations` package docstring are updated. No behavior change.
