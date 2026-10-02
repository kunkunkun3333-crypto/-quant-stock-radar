"""固定、未用歷史勝率調優的 V5.2 研究設定。"""
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Settings:
    model_version: str = '5.2-manual-20261002'
    period: str = '10y'
    batch_size: int = 20
    retries: int = 2
    max_failed_batches: int = 3
    request_pause: float = 0.0
    scan_network_budget_seconds: int = 120
    history_ttl: int = 21600
    fundamental_ttl: int = 86400
    fallback_max_age: int = 604800
    minimum_bars: int = 80
    min_peers: int = 5
    min_industry_coverage: float = 0.60
    min_data_coverage: float = 70.0
    stale_days: int = 7
    quality_weights: dict = field(default_factory=lambda: {'Revenue Growth':15,'Monthly Revenue YoY':10,'EPS':10,'EPS Growth':15,'ROE':15,'Gross Margin':10,'Operating Margin':10,'P/E':10,'Free Cash Flow':5})
    quality_ranges: dict = field(default_factory=lambda: {'Revenue Growth':(-.1,.3),'Monthly Revenue YoY':(-.1,.3),'EPS Growth':(-.2,.4),'ROE':(0.,.25),'Gross Margin':(0.,.6),'Operating Margin':(-.05,.25)})
    entry_weights: dict = field(default_factory=lambda: {'RSI':20,'BIAS':20,'Trend':25,'Support':15,'Volume':10,'Momentum':10})
    risk_weights: dict = field(default_factory=lambda: {'Volatility':20,'ATR':15,'Drawdown':15,'Surge':10,'Volume':10,'Trend':10,'Fundamentals':5,'Valuation':5,'Missing':10})
    industry_weights: dict = field(default_factory=lambda: {'Return20':30,'Return60':20,'Relative20':30,'StrongRatio':20})
    total_weights: dict = field(default_factory=lambda: {'Quality Score':30,'Entry Score':30,'Trend Score':15,'Industry Strength':10,'Institutional Score':10,'Risk Score':5})
    high: dict = field(default_factory=lambda: {'quality':75,'entry':80,'industry':60,'risk':50,'samples':30,'winrate':.60,'quality_coverage':60})
    historical_entry_threshold: float = 65.
    bear_entry_penalty: float = 15.
    neutral_entry_penalty: float = 5.
    unknown_entry_penalty: float = 10.
    bearish_entry_cap: float = 35.
    oversold_entry_cap: float = 45.
    oos_fraction: float = .30
    horizons: tuple = (5,20,60)
    roundtrip_cost_bps: float = 50.
    # 不以技術訊號回測冒充完整多因子驗證。
    full_model_validated: bool = False

DEFAULT = Settings()
