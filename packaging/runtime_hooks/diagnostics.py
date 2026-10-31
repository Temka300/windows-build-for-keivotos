"""Install safe early crash reporting before the frozen entry point imports."""
from services.log_bootstrap import install
install()
