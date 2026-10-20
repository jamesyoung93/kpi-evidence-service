GRANT USE SCHEMA, SELECT, MODIFY ON SCHEMA ${schema} TO `${app_sp}`;
GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA ${gov} TO `${app_sp}`;

GRANT USE SCHEMA ON SCHEMA ${schema} TO `${consumer_group}`;
GRANT SELECT ON TABLE ${schema}.published_kpis_current TO `${consumer_group}`;
GRANT SELECT ON TABLE ${schema}.kpi_registry TO `${consumer_group}`;
GRANT SELECT ON TABLE ${schema}.platform_kpi_values TO `${consumer_group}`;
GRANT SELECT ON TABLE ${schema}.kpi_status_overview TO `${consumer_group}`;

GRANT USE SCHEMA ON SCHEMA ${schema} TO `${reviewer_group}`;
GRANT SELECT ON TABLE ${schema}.published_kpis_current TO `${reviewer_group}`;
GRANT SELECT ON TABLE ${schema}.kpi_results_current TO `${reviewer_group}`;
GRANT SELECT ON TABLE ${schema}.review_open TO `${reviewer_group}`;
GRANT SELECT ON TABLE ${schema}.dq_results TO `${reviewer_group}`;
GRANT SELECT ON TABLE ${schema}.kpi_registry TO `${reviewer_group}`;
GRANT SELECT ON TABLE ${schema}.platform_kpi_values TO `${reviewer_group}`;
GRANT SELECT ON TABLE ${schema}.kpi_status_overview TO `${reviewer_group}`;
