# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "5b12a53a-c24e-4970-9513-f30ac993ffe1",
# META       "default_lakehouse_name": "lh_finance",
# META       "default_lakehouse_workspace_id": "d222c4dd-097c-49d7-bd72-606351afdd7f"
# META     }
# META   }
# META }

# MARKDOWN ********************

# Fabric notebook source (PySpark) - converted to .ipynb by infra/finance_lakehouse.py
# Default lakehouse: lh_finance (ce-finance-<env>). Idempotent: every table is fully rebuilt.
# 
# Bronze (no copies):  bronze_customers / bronze_contracts / bronze_payments  -> shortcuts to mir_loan_servicing (open mirroring)
#                      ref_sites                                              -> shortcut to lh_asset.sites (Asset domain)
#                      Files/landing/credit_bureau/*.csv                      -> partner file drop
# Silver:              typed, deduplicated, conformed; credit score history as SCD2
# Gold:                star schema for the semantic model (step 4) + data-quality results

# MARKDOWN ********************

# # Finance medallion (Contoso Energy, synthetic data)
# Bronze shortcuts -> silver -> gold star schema. Run on a schedule after the mirror and the partner drop.

# CELL ********************

from pyspark.sql import Window
from pyspark.sql import functions as F

run_ts = F.current_timestamp()
dq = []  # (check, value, passed)


def save(df, name):
    df.withColumn("_loaded_at", run_ts).write.mode("overwrite").option("overwriteSchema", "true").format("delta").saveAsTable(name)
    return spark.table(name)

# MARKDOWN ********************

# ## Bronze: credit bureau partner files -> `bronze_credit_bureau` (append-only history, with lineage)

# CELL ********************

bureau_raw = (spark.read.option("header", True).csv("Files/landing/credit_bureau/*.csv")
              .withColumn("source_file", F.element_at(F.split(F.col("_metadata.file_path"), "/"), -1)))
bronze_bureau = save(bureau_raw, "bronze_credit_bureau")
dq.append(("bronze_credit_bureau_files", bronze_bureau.select("source_file").distinct().count(), True))

# MARKDOWN ********************

# ## Silver

# CELL ********************

# Customers: mirrored source + current bureau score (latest month, latest corrected file version)
bureau = (bronze_bureau
          .select("customer_id", F.to_date("as_of_date").alias("as_of_date"),
                  F.col("bureau_score").cast("int").alias("bureau_score"), "score_band",
                  F.col("inquiries_6m").cast("int").alias("inquiries_6m"),
                  F.col("delinquencies_24m").cast("int").alias("delinquencies_24m"),
                  (F.col("bankruptcy_flag") == "True").alias("bankruptcy_flag"),
                  F.col("file_version").cast("int").alias("file_version"), "source_file")
          .withColumn("rn", F.row_number().over(Window.partitionBy("customer_id", "as_of_date").orderBy(F.desc("file_version"))))
          .filter("rn = 1").drop("rn"))

# SCD2 credit history: one row per customer per bureau month, valid until the next refresh
w = Window.partitionBy("customer_id").orderBy("as_of_date")
credit_history = (bureau
                  .withColumn("valid_from", F.col("as_of_date"))
                  .withColumn("valid_to", F.date_sub(F.lead("as_of_date").over(w), 1))
                  .withColumn("is_current", F.col("valid_to").isNull())
                  .withColumn("score_change", F.col("bureau_score") - F.lag("bureau_score").over(w)))
save(credit_history, "silver_customer_credit_history")

current_score = credit_history.filter("is_current").select(
    "customer_id", F.col("bureau_score").alias("bureau_score_current"),
    F.col("score_band").alias("bureau_band_current"), F.col("as_of_date").alias("bureau_as_of"))

customers = (spark.table("bronze_customers")
             .select("customer_id", "first_name", "last_name", "email", "phone", "street_address", "city", "region",
                     "postal_code", F.col("credit_band").alias("credit_band_onboarding"),
                     F.col("credit_score").cast("int").alias("credit_score_onboarding"),
                     F.col("signup_date").cast("date").alias("signup_date"), "segment",
                     F.col("marketing_opt_in").cast("boolean").alias("marketing_opt_in"), "updated_at")
             .join(current_score, "customer_id", "left"))
silver_customers = save(customers, "silver_customers")

# Contracts: typed, term end, and a flag for contracts whose customer was erased in the source
contracts = (spark.table("bronze_contracts")
             .select("contract_id", "customer_id", "site_id", "product", F.col("start_date").cast("date").alias("start_date"),
                     F.col("term_months").cast("int").alias("term_months"),
                     *[F.col(c).cast("decimal(12,2)").alias(c) for c in ("system_cost", "principal", "monthly_payment")],
                     F.col("apr_pct").cast("decimal(5,2)").alias("apr_pct"),
                     F.col("ppa_rate_per_kwh").cast("decimal(6,4)").alias("ppa_rate_per_kwh"),
                     F.col("escalator_pct").cast("decimal(4,2)").alias("escalator_pct"), "currency", "status", "updated_at")
             .withColumn("term_end_date", F.expr("add_months(start_date, term_months)"))
             .join(silver_customers.select("customer_id", F.lit(True).alias("customer_exists")), "customer_id", "left")
             .withColumn("customer_exists", F.coalesce("customer_exists", F.lit(False))))
silver_contracts = save(contracts, "silver_contracts")
dq.append(("contracts_with_erased_customer", silver_contracts.filter("NOT customer_exists").count(), True))

# Payments: the servicing feed re-posts some payments -> keep the latest recorded version per payment_id
raw_pay = spark.table("bronze_payments")
w = Window.partitionBy("payment_id").orderBy(F.desc("recorded_at"), F.desc("payment_txn_id"))
payments = (raw_pay
            .withColumn("rn", F.row_number().over(w)).filter("rn = 1").drop("rn")
            .select("payment_id", "contract_id", "customer_id", F.col("installment_no").cast("int").alias("installment_no"),
                    F.col("due_date").cast("date").alias("due_date"), F.col("paid_date").cast("date").alias("paid_date"),
                    F.col("amount_due").cast("decimal(10,2)").alias("amount_due"),
                    F.coalesce(F.col("amount_paid"), F.lit(0)).cast("decimal(10,2)").alias("amount_paid"),
                    "method", "status", "recorded_at")
            .withColumn("days_late", F.when(F.col("paid_date").isNotNull(), F.greatest(F.datediff("paid_date", "due_date"), F.lit(0))))
            .withColumn("is_late_arrival", F.datediff(F.to_date("recorded_at"), F.coalesce("paid_date", "due_date")) > 7)
            .withColumn("is_live_txn", F.col("installment_no") == 0))
silver_payments = save(payments, "silver_payments")
dups = raw_pay.count() - silver_payments.count()
dq.append(("payment_duplicates_removed", dups, dups >= 0))
dq.append(("payments_late_arrival", silver_payments.filter("is_late_arrival").count(), True))

# MARKDOWN ********************

# ## Gold: star schema (semantic model in step 4)

# CELL ********************

bounds = silver_payments.agg(F.trunc(F.min("due_date"), "month").alias("lo"), F.max("due_date").alias("hi")).first()
dim_date = (spark.sql(f"SELECT explode(sequence(date'{bounds.lo}', last_day(date'{bounds.hi}'), interval 1 day)) AS date")
            .select(F.date_format("date", "yyyyMMdd").cast("int").alias("date_key"), "date",
                    F.year("date").alias("year"), F.quarter("date").alias("quarter"), F.month("date").alias("month"),
                    F.date_format("date", "MMM").alias("month_name"), F.date_format("date", "yyyy-MM").alias("year_month"),
                    (F.col("date") == F.last_day("date")).alias("is_month_end")))
save(dim_date, "gold_dim_date")

# PII stays in gold_dim_customer on purpose: it gets sensitivity labels / column-level security in steps 8-9.
dim_customer = silver_customers.select(
    "customer_id", F.concat_ws(" ", "first_name", "last_name").alias("customer_name"), "email", "phone", "city", "region",
    "segment", "signup_date", "credit_band_onboarding", "bureau_score_current", "bureau_band_current", "marketing_opt_in")
save(dim_customer, "gold_dim_customer")

sites = spark.table("ref_sites").select("site_id", F.col("pv_capacity_kw").cast("double").alias("pv_capacity_kw"),
                                        (F.col("has_battery").cast("string") == "True").alias("has_battery"),
                                        F.col("region").alias("site_region"))
dim_contract = silver_contracts.join(sites, "site_id", "left").select(
    "contract_id", "customer_id", "site_id", "site_region", "product", "start_date", "term_end_date", "term_months",
    "system_cost", "principal", "apr_pct", "monthly_payment", "status", "pv_capacity_kw", "has_battery", "customer_exists")
save(dim_contract, "gold_dim_contract")

fact_payment = silver_payments.select(
    "payment_id", "contract_id", "customer_id", "installment_no",
    F.date_format("due_date", "yyyyMMdd").cast("int").alias("due_date_key"),
    F.date_format("paid_date", "yyyyMMdd").cast("int").alias("paid_date_key"),
    "due_date", "paid_date", "amount_due", "amount_paid", (F.col("amount_due") - F.col("amount_paid")).alias("shortfall"),
    "status", "method", "days_late", "is_late_arrival")
save(fact_payment, "gold_fact_payment")

# Monthly contract snapshot: arrears = cumulative (due - paid); days past due bucket for delinquency KPIs
monthly = (silver_payments.filter("NOT is_live_txn")
           .withColumn("month", F.trunc("due_date", "month"))
           .groupBy("contract_id", "month")
           .agg(F.sum("amount_due").alias("amount_due"), F.sum("amount_paid").alias("amount_paid"),
                F.min("due_date").alias("due_date"),
                F.max(F.coalesce("days_late", F.lit(0))).alias("max_days_late"),
                F.max(F.when(F.col("paid_date").isNull(), 1).otherwise(0)).alias("has_unpaid")))
wc = Window.partitionBy("contract_id").orderBy("month").rowsBetween(Window.unboundedPreceding, 0)
# Consecutive unpaid months form a streak; DPD at month end counts from the streak's first unpaid due date.
monthly = monthly.withColumn("streak", F.sum(1 - F.col("has_unpaid")).over(wc))
ws_ = Window.partitionBy("contract_id", "streak").orderBy("month").rowsBetween(Window.unboundedPreceding, 0)
contract_month = (monthly
                  .withColumn("streak_start_due", F.min(F.when(F.col("has_unpaid") == 1, F.col("due_date"))).over(ws_))
                  .withColumn("arrears", F.round(F.sum(F.col("amount_due") - F.col("amount_paid")).over(wc), 2))
                  .withColumn("days_past_due", F.when(F.col("has_unpaid") == 1,
                                                      F.datediff(F.least(F.last_day("month"), F.current_date()), "streak_start_due"))
                              .otherwise(F.col("max_days_late")))
                  .withColumn("days_past_due", F.greatest(F.col("days_past_due"), F.lit(0)))
                  .withColumn("dpd_bucket", F.when(F.col("days_past_due") == 0, "Current")
                              .when(F.col("days_past_due") < 30, "1-29").when(F.col("days_past_due") < 60, "30-59")
                              .when(F.col("days_past_due") < 90, "60-89").otherwise("90+"))
                  # Industry convention: delinquent = 30+ days past due (small partial-payment arrears alone are not).
                  .withColumn("is_delinquent", F.col("days_past_due") >= 30)
                  .withColumn("month_key", F.date_format("month", "yyyyMMdd").cast("int"))
                  .drop("streak", "streak_start_due"))
save(contract_month, "gold_fact_contract_month")

# MARKDOWN ********************

# ## Data quality results (read by infra/finance_lakehouse.py verify)

# CELL ********************

gold_counts = {t: spark.table(t).count() for t in ("gold_dim_customer", "gold_dim_contract", "gold_fact_payment", "gold_fact_contract_month", "gold_dim_date")}
dq += [(f"rows_{k}", v, v > 0) for k, v in gold_counts.items()]
pk_dups = fact_payment.groupBy("payment_id").count().filter("count > 1").count()
dq.append(("gold_fact_payment_pk_duplicates", pk_dups, pk_dups == 0))
orphans = fact_payment.join(dim_contract, "contract_id", "left_anti").count()
dq.append(("gold_fact_payment_orphans", orphans, orphans == 0))
dq_df = spark.createDataFrame([(c, float(v), bool(p)) for c, v, p in dq], "check string, value double, passed boolean")
save(dq_df, "gold_dq_results")
display(dq_df)
failed = [c for c, v, p in dq if not p]
assert not failed, f"data quality checks failed: {failed}"
