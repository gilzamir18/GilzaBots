"""Cloud storage backends implementing `gilzabots.cloud.base.CloudStorageProvider`.

Modules here are imported lazily by `gilzabots.cloud.registry`, so a backend with
an unmet optional dependency never breaks application start-up.
"""
