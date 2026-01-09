"""Forms for clinker supply chain operations."""
from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    HiddenField,
    IntegerField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    widgets,
)
from wtforms.validators import InputRequired, Length, NumberRange, Optional, ValidationError


PERIOD_CHOICES = [(1, "1"), (2, "2"), (3, "3")]


class WorkspaceForm(FlaskForm):
    name = StringField("Workspace Name", validators=[InputRequired(), Length(min=2, max=120)])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Create workspace")


class PlantForm(FlaskForm):
    plant_name = StringField("Plant Name", validators=[InputRequired(), Length(min=2, max=255)])
    plant_type = SelectField(
        "Plant Type",
        choices=[("IU", "Integrated Unit"), ("GU", "Grinding Unit")],
        validators=[InputRequired()],
        default="IU",
    )
    location = StringField("Location", validators=[InputRequired(), Length(min=2, max=255)])
    production_capacity = DecimalField(
        "Production Capacity",
        validators=[InputRequired(), NumberRange(min=0)],
        places=2,
    )
    consumption_capacity = DecimalField(
        "Consumption Capacity",
        validators=[InputRequired(), NumberRange(min=0)],
        places=2,
    )
    max_inventory_capacity = DecimalField(
        "Max Inventory Capacity",
        validators=[InputRequired(), NumberRange(min=0)],
        places=2,
    )
    safety_stock_level = DecimalField(
        "Safety Stock Level",
        validators=[InputRequired(), NumberRange(min=0)],
        places=2,
    )
    status = SelectField(
        "Status",
        choices=[("active", "Active"), ("disabled", "Disabled")],
        validators=[InputRequired()],
        default="active",
    )
    submit = SubmitField("Save Plant")

    def validate_safety_stock_level(self, field):  # noqa: D401
        """Ensure safety stock does not exceed the max capacity."""
        if self.max_inventory_capacity.data is None:
            return
        if field.data is not None and field.data > self.max_inventory_capacity.data:
            raise ValidationError("Safety stock must be below the max inventory capacity.")


class PlantDemandForm(FlaskForm):
    plant_id = SelectField("IUGU Code", coerce=int, validators=[InputRequired()])
    time_period = SelectField(
        "Time Period",
        coerce=int,
        choices=PERIOD_CHOICES,
        validators=[InputRequired(), NumberRange(min=1, max=3)],
        default=1,
    )
    demand = DecimalField("Demand (tons)", validators=[InputRequired(), NumberRange(min=0.01)], places=2)
    min_fulfillment_pct = DecimalField(
        "Min fulfillment (%)",
        validators=[Optional(), NumberRange(min=0, max=100)],
        places=2,
        default=None,
    )
    submit = SubmitField("Save demand")


class TransportRouteForm(FlaskForm):
    source_plant_id = SelectField("Source Plant", coerce=int, validators=[InputRequired()])
    destination_plant_id = SelectField("Destination Plant", coerce=int, validators=[InputRequired()])
    mode = SelectField(
        "Mode",
        choices=[("Road", "Road"), ("Rail", "Rail"), ("Sea", "Sea")],
        validators=[InputRequired()],
    )
    trip_capacity = DecimalField("Trip Capacity", validators=[InputRequired(), NumberRange(min=0.01)], places=2)
    min_batch_quantity = DecimalField("Min Batch Quantity", validators=[InputRequired(), NumberRange(min=0)], places=2)
    max_trips_per_period = IntegerField("Max Trips / Period", validators=[InputRequired(), NumberRange(min=0)])
    cost_per_trip = DecimalField("Cost per Trip", validators=[InputRequired(), NumberRange(min=0)], places=2)
    status = SelectField(
        "Status",
        choices=[("active", "Active"), ("disabled", "Disabled")],
        validators=[InputRequired()],
        default="active",
    )
    submit = SubmitField("Save Route")

    def validate_trip_capacity(self, field):  # noqa: D401
        """Enforce trip capacity greater than minimum batch quantity."""
        if self.min_batch_quantity.data is None:
            return
        if field.data is not None and field.data <= self.min_batch_quantity.data:
            raise ValidationError("Trip capacity must exceed the minimum batch quantity.")


class InventoryUpdateForm(FlaskForm):
    plant_id = SelectField("Plant", coerce=int, validators=[InputRequired()])
    current_inventory = DecimalField(
        "Current Inventory",
        validators=[InputRequired(), NumberRange(min=0)],
        places=2,
    )
    submit = SubmitField("Update Inventory")


class ScenarioForm(FlaskForm):
    scenario_name = StringField("Scenario Name", validators=[InputRequired(), Length(min=2, max=255)])
    periods = IntegerField("Periods", validators=[InputRequired(), NumberRange(min=1, max=36)])
    status = SelectField(
        "Status",
        choices=[("draft", "Draft"), ("executed", "Executed"), ("completed", "Completed")],
        default="draft",
        validators=[InputRequired()],
    )
    workspace_id = HiddenField("Workspace ID", validators=[Optional()])
    submit = SubmitField("Save Scenario")


class OptimizationRunForm(FlaskForm):
    scenario_id = SelectField("Scenario", coerce=int, validators=[InputRequired()])
    mode = SelectField(
        "Mode",
        choices=[
            ("elastic", "Elastic (default)"),
            ("deterministic", "Deterministic (alias)"),
        ],
        validators=[InputRequired()],
        default="elastic",
    )
    runtime_limit = IntegerField("Time limit (s)", validators=[Optional(), NumberRange(min=1, max=600)])
    demand_uplift_pct = DecimalField("Demand buffer %", validators=[Optional(), NumberRange(min=0, max=1)], places=2, default=0)
    scenario_samples = IntegerField("Scenario samples", validators=[Optional(), NumberRange(min=1, max=10)])
    strict_service = BooleanField("Strict service (no stockouts)", default=True)
    allow_shortage_penalties = BooleanField("Allow shortage penalties", default=False)
    shortage_penalty = DecimalField("Shortage penalty / unit", validators=[Optional(), NumberRange(min=0)], places=2, default=1000)
    service_level_target = DecimalField("Service level target (0-1)", validators=[Optional(), NumberRange(min=0, max=1)], places=2, default=0.95)
    mark_completed = BooleanField("Mark scenario completed on success", default=False)
    workspace_id = HiddenField("Workspace ID", validators=[Optional()])
    submit = SubmitField("Run optimization")


class CsvExportForm(FlaskForm):
    dataset = SelectField(
        "Dataset",
        choices=[
            ("plants", "Plants"),
            ("transport_routes", "Transport Routes"),
            ("inventory", "Inventory"),
            ("scenarios", "Scenario Summary"),
        ],
        validators=[InputRequired()],
    )
    include_inactive = BooleanField("Include inactive records")
    submit = SubmitField("Export CSV")

    _allowed_datasets = {"plants", "transport_routes", "inventory", "scenarios"}

    def validate_dataset(self, field):  # noqa: D401
        """Keep exports constrained to supported datasets."""
        if field.data not in self._allowed_datasets:
            raise ValidationError("Unsupported export target selected.")


class CsvUploadForm(FlaskForm):
    workspace_id = HiddenField("Workspace ID", validators=[Optional()])
    dataset_id = HiddenField("Dataset ID", validators=[Optional()])
    file = FileField(
        "CSV file",
        validators=[FileRequired(message="Please choose a CSV file."), FileAllowed(["csv"], "CSV files only.")],
    )
    submit = SubmitField("Upload CSV")


class PdfReportForm(FlaskForm):
    report_type = SelectField(
        "Report",
        choices=[
            ("inventory_health", "Inventory Health"),
            ("transport_network", "Transport Network Summary"),
        ],
        validators=[InputRequired()],
    )
    timeframe = SelectField(
        "Timeframe",
        choices=[
            ("current", "Current Snapshot"),
            ("last_30_days", "Last 30 Days"),
            ("last_quarter", "Last Quarter"),
        ],
        default="current",
        validators=[InputRequired()],
    )
    highlight_alerts = BooleanField("Highlight safety alerts", default=True)
    submit = SubmitField("Generate PDF")


class OptimizationExportForm(FlaskForm):
    scenario_id = SelectField("Scenario", coerce=int, validators=[InputRequired()])
    sections = SelectMultipleField(
        "Sections",
        choices=[
            ("summary", "Summary KPIs"),
            ("dispatch", "Dispatch plan"),
            ("production", "Production plan"),
            ("inventory", "Inventory ledger"),
        ],
        default=["summary", "dispatch", "production", "inventory"],
        option_widget=widgets.CheckboxInput(),
        widget=widgets.ListWidget(prefix_label=False),
        validators=[InputRequired()],
    )
    submit_csv = SubmitField("Export CSV")
    submit_pdf = SubmitField("Export PDF")


class ActivityLogFilterForm(FlaskForm):
    action_type = SelectField(
        "Action",
        choices=[
            ("", "Any"),
            ("plant", "Plant change"),
            ("transport", "Transport change"),
            ("inventory", "Inventory update"),
            ("scenario", "Scenario event"),
            ("user", "User management"),
        ],
        default="",
        validators=[Optional()],
    )
    entity_type = SelectField(
        "Entity",
        choices=[
            ("", "Any"),
            ("plant", "Plant"),
            ("route", "Transport Route"),
            ("inventory", "Inventory"),
            ("scenario", "Scenario"),
            ("user", "User"),
        ],
        default="",
        validators=[Optional()],
    )
    user_id = IntegerField("User", validators=[Optional(), NumberRange(min=1)])
    severity = SelectField(
        "Severity",
        choices=[("", "Any"), ("info", "Info"), ("warning", "Warning"), ("critical", "Critical")],
        default="",
        validators=[Optional()],
    )
    start_date = DateField("From", validators=[Optional()])
    end_date = DateField("To", validators=[Optional()])
    search = StringField("Search", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Filter Logs")

    def validate_end_date(self, field):  # noqa: D401
        """Ensure date windows make sense."""
        if field.data and self.start_date.data and field.data < self.start_date.data:
            raise ValidationError("End date must be after start date.")


class AuditTrailRequestForm(FlaskForm):
    entity_type = SelectField(
        "Entity",
        choices=[
            ("plant", "Plant"),
            ("route", "Transport Route"),
            ("inventory", "Inventory"),
            ("scenario", "Scenario"),
            ("user", "User"),
        ],
        validators=[InputRequired()],
    )
    entity_id = IntegerField("Record ID", validators=[InputRequired(), NumberRange(min=1)])
    include_changes = BooleanField("Include change summary", default=True)
    submit = SubmitField("View Audit Trail")


class PlantFilterForm(FlaskForm):
    plant_type = SelectField(
        "Plant Type",
        choices=[("", "Any"), ("IU", "Integrated Unit"), ("GU", "Grinding Unit")],
        default="",
        validators=[Optional()],
    )
    status = SelectField(
        "Status",
        choices=[("", "Any"), ("active", "Active"), ("disabled", "Disabled")],
        default="",
        validators=[Optional()],
    )
    region = StringField("Region", validators=[Optional(), Length(max=120)])
    search = StringField("Search", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Apply Filters")


class TransportFilterForm(FlaskForm):
    mode = SelectField(
        "Mode",
        choices=[("", "Any"), ("Road", "Road"), ("Rail", "Rail"), ("Sea", "Sea")],
        default="",
        validators=[Optional()],
    )
    status = SelectField(
        "Status",
        choices=[("", "Any"), ("active", "Active"), ("disabled", "Disabled")],
        default="",
        validators=[Optional()],
    )
    min_cost = DecimalField("Min Cost", validators=[Optional(), NumberRange(min=0)], places=2)
    max_cost = DecimalField("Max Cost", validators=[Optional(), NumberRange(min=0)], places=2)
    active_only = BooleanField("Active routes only", default=False)
    submit = SubmitField("Apply Filters")

    def validate_max_cost(self, field):  # noqa: D401
        """Guard against inverted cost ranges."""
        if field.data is not None and self.min_cost.data is not None and field.data < self.min_cost.data:
            raise ValidationError("Max cost must be greater than min cost.")


class InventoryFilterForm(FlaskForm):
    below_safety_only = BooleanField("Below safety stock", default=False)
    critical_only = BooleanField("Critical alerts", default=False)
    sort_by = SelectField(
        "Sort",
        choices=[("recent", "Recently updated"), ("level_high", "Highest inventory"), ("level_low", "Lowest inventory")],
        default="recent",
        validators=[InputRequired()],
    )
    submit = SubmitField("Apply Filters")


class NotificationInboxFilterForm(FlaskForm):
    severity = SelectField(
        "Severity",
        choices=[("", "Any"), ("info", "Info"), ("warning", "Warning"), ("critical", "Critical"), ("system", "System")],
        default="",
        validators=[Optional()],
    )
    unread_only = BooleanField("Unread only", default=True)
    acknowledge_all = BooleanField("Mark all as read")
    submit = SubmitField("Update Notifications")


class IUGUTypeForm(FlaskForm):
    code = StringField("IUGU Code", validators=[InputRequired(), Length(min=1, max=16)])
    plant_type = SelectField(
        "Plant Type",
        choices=[("IU", "Integrated Unit"), ("GU", "Grinding Unit")],
        validators=[InputRequired()],
    )
    sources_count = IntegerField("Sources count", validators=[Optional(), NumberRange(min=0)])
    submit = SubmitField("Save IUGU type")


class ClinkerDemandInputForm(FlaskForm):
    plant_code = StringField("IUGU Code", validators=[InputRequired(), Length(min=1, max=16)])
    time_period = SelectField(
        "Period",
        coerce=int,
        choices=PERIOD_CHOICES,
        validators=[InputRequired(), NumberRange(min=1, max=3)],
        default=1,
    )
    demand_tons = DecimalField("Demand (tons)", validators=[InputRequired(), NumberRange(min=0.01)], places=2)
    min_fulfillment_pct = DecimalField(
        "Min fulfill %",
        validators=[Optional(), NumberRange(min=0, max=100)],
        places=2,
        default=100,
    )
    submit = SubmitField("Save demand")


class ClinkerCapacityForm(FlaskForm):
    plant_code = StringField("IUGU Code", validators=[InputRequired(), Length(min=1, max=16)])
    time_period = SelectField(
        "Period",
        coerce=int,
        choices=PERIOD_CHOICES,
        validators=[InputRequired(), NumberRange(min=1, max=3)],
        default=1,
    )
    capacity_tons = DecimalField("Capacity (tons)", validators=[InputRequired(), NumberRange(min=0)], places=2)
    submit = SubmitField("Save capacity")


class ProductionCostForm(FlaskForm):
    plant_code = StringField("IUGU Code", validators=[InputRequired(), Length(min=1, max=16)])
    time_period = SelectField(
        "Period",
        coerce=int,
        choices=PERIOD_CHOICES,
        validators=[InputRequired(), NumberRange(min=1, max=3)],
        default=1,
    )
    cost_per_ton = DecimalField("Cost per ton", validators=[InputRequired(), NumberRange(min=0.01)], places=2)
    submit = SubmitField("Save cost")


class LogisticsIUGUForm(FlaskForm):
    from_code = StringField("From code", validators=[InputRequired(), Length(min=1, max=16)])
    to_code = StringField("To code", validators=[InputRequired(), Length(min=1, max=16)])
    transport_code = StringField("Transport code", validators=[InputRequired(), Length(min=1, max=10)])
    time_period = SelectField(
        "Period",
        coerce=int,
        choices=PERIOD_CHOICES,
        validators=[InputRequired(), NumberRange(min=1, max=3)],
        default=1,
    )
    freight_cost = DecimalField("Freight cost", validators=[InputRequired(), NumberRange(min=0)], places=2)
    handling_cost = DecimalField("Handling cost", validators=[Optional(), NumberRange(min=0)], places=2, default=0)
    quantity_multiplier = DecimalField(
        "Quantity multiplier",
        validators=[InputRequired(), NumberRange(min=0.0001)],
        places=2,
        default=1,
    )
    submit = SubmitField("Save route")


class IUGUConstraintForm(FlaskForm):
    from_code = StringField("From code", validators=[InputRequired(), Length(min=1, max=16)])
    transport_code = StringField("Transport code", validators=[Optional(), Length(max=10)])
    to_code = StringField("To code", validators=[Optional(), Length(max=16)])
    time_period = SelectField(
        "Period",
        coerce=int,
        choices=PERIOD_CHOICES,
        validators=[InputRequired(), NumberRange(min=1, max=3)],
        default=1,
    )
    constraint_type = SelectField(
        "Constraint",
        choices=[("L", "<= (Upper)"), ("G", ">= (Lower)"), ("E", "= (Exact)")],
        validators=[InputRequired()],
    )
    value_type = SelectField(
        "Value type",
        choices=[("C", "Capacity")],
        validators=[InputRequired()],
        default="C",
    )
    value = DecimalField("Value", validators=[InputRequired(), NumberRange(min=0)], places=2)
    submit = SubmitField("Save constraint")


class IUGUOpeningStockForm(FlaskForm):
    plant_code = StringField("IUGU Code", validators=[InputRequired(), Length(min=1, max=16)])
    opening_stock = DecimalField("Opening stock", validators=[InputRequired(), NumberRange(min=0)], places=2)
    submit = SubmitField("Save opening stock")


class HubOpeningStockForm(FlaskForm):
    from_code = StringField("From code", validators=[InputRequired(), Length(min=1, max=16)])
    to_code = StringField("To code", validators=[InputRequired(), Length(min=1, max=16)])
    opening_stock = DecimalField("Opening stock", validators=[InputRequired(), NumberRange(min=0)], places=2)
    submit = SubmitField("Save hub stock")


class IUGUClosingStockForm(FlaskForm):
    plant_code = StringField("IUGU Code", validators=[InputRequired(), Length(min=1, max=16)])
    time_period = SelectField(
        "Period",
        coerce=int,
        choices=PERIOD_CHOICES,
        validators=[InputRequired(), NumberRange(min=1, max=3)],
        default=1,
    )
    min_close_stock = DecimalField("Min close", validators=[InputRequired(), NumberRange(min=0)], places=2)
    max_close_stock = DecimalField("Max close", validators=[Optional(), NumberRange(min=0)], places=2)
    submit = SubmitField("Save closing stock")
