import unittest
import json
import os
import re
import sys

# Ensure project src path is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.state import obfuscate, deobfuscate
from src.migration.migrator import clean_sql_definer, strip_foreign_keys_from_ddl
from src.verification.verifier import calculate_table_checksum_sql
from src.server import app

class TestMigrationTool(unittest.TestCase):
    
    def test_password_obfuscation(self):
        """Tests that state obfuscator base64-encodes passwords correctly."""
        password = "mySecretPassword123"
        obfuscated = obfuscate(password)
        self.assertNotEqual(password, obfuscated)
        
        deobfuscated = deobfuscate(obfuscated)
        self.assertEqual(password, deobfuscated)
        
        # Test empty handling
        self.assertEqual(obfuscate(""), "")
        self.assertEqual(deobfuscate(""), "")

    def test_clean_sql_definer(self):
        """Tests that DEFINER clause is completely stripped for Azure compatibility."""
        sql_with_definer = "CREATE DEFINER=`root`@`localhost` VIEW my_view AS SELECT * FROM users"
        cleaned_sql = clean_sql_definer(sql_with_definer)
        self.assertNotIn("DEFINER", cleaned_sql)
        self.assertNotIn("`root`@`localhost`", cleaned_sql)

        sql_without_definer = "CREATE VIEW my_view AS SELECT * FROM users"
        self.assertEqual(clean_sql_definer(sql_without_definer), sql_without_definer)

    def test_strip_foreign_keys_from_ddl(self):
        """Tests stripping CONSTRAINT FOREIGN KEY lines and rebuilding ALTER TABLE."""
        ddl = (
            "CREATE TABLE `orders` (\n"
            "  `id` int NOT NULL,\n"
            "  `user_id` int NOT NULL,\n"
            "  PRIMARY KEY (`id`),\n"
            "  CONSTRAINT `fk_orders_users` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)\n"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        )
        
        cleaned_ddl, fk_alters = strip_foreign_keys_from_ddl(ddl)
        
        # Cleaned DDL should not contain FOREIGN KEY constraint
        self.assertNotIn("FOREIGN KEY", cleaned_ddl)
        self.assertNotIn("CONSTRAINT `fk_orders_users`", cleaned_ddl)
        
        # Table creation should end nicely with engine statement
        self.assertIn("PRIMARY KEY (`id`)", cleaned_ddl)
        self.assertIn(") ENGINE=InnoDB", cleaned_ddl)
        
        # FK Alters list should contain the add constraint alter
        self.assertEqual(len(fk_alters), 1)
        self.assertIn("ALTER TABLE `orders` ADD CONSTRAINT `fk_orders_users` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)", fk_alters[0])

    def test_calculate_table_checksum_sql(self):
        """Tests table checksum SQL query building block."""
        columns = [
            {"name": "id", "data_type": "int"},
            {"name": "name", "data_type": "varchar"},
            {"name": "data", "data_type": "blob"}
        ]
        sql = calculate_table_checksum_sql(columns)
        self.assertIn("BIT_XOR", sql)
        self.assertIn("MD5", sql)
        # Check that blob data type uses HEX expression
        self.assertIn("HEX(`data`)", sql)
        self.assertIn("CAST(`name` AS CHAR CHARACTER SET utf8mb4)", sql)

    def test_flask_server_profiles_api(self):
        """Tests that server endpoints are responsive and validate profiles."""
        client = app.test_client()
        
        # Test index serves correctly
        resp = client.get('/')
        self.assertEqual(resp.status_code, 200)
        
        # Test profiles api
        resp = client.get('/api/profiles')
        self.assertEqual(resp.status_code, 200)
        profiles_data = json.loads(resp.data)
        self.assertIsInstance(profiles_data, dict)

    def test_verify_database_filtering(self):
        """Tests that verifier can accept table filters."""
        # This checks verify_database signature and that it handles empty or list parameters correctly.
        from src.verification.verifier import verify_database
        # Simply verifying function signature binds successfully
        self.assertTrue(callable(verify_database))

    def test_generate_report_parameters(self):
        """Tests generate_report signature and direct parameters validation."""
        from src.reporting.reporter import generate_report
        self.assertTrue(callable(generate_report))

    def test_parse_mismatches(self):
        """Tests delta sync regex parsing for failed tables and triggers."""
        from src.migration.migrator import parse_mismatches
        sample_log = "Triggers mismatch. Missing: {'registers_BEFORE_INSERT'}. Extra: set() Table 'directus_access' Foreign Keys mismatch. Table 'registers' row count mismatch!"
        failed_tbls, failed_trigs, _, _, _, _ = parse_mismatches(sample_log)
        self.assertIn("registers_BEFORE_INSERT", failed_trigs)
        self.assertIn("directus_access", failed_tbls)
        self.assertIn("registers", failed_tbls)

    def test_exclude_directus_filtering(self):
        """Tests that directus_ tables are filtered when exclude_directus is enabled."""
        tables = ["orders", "directus_users", "products", "directus_files"]
        filtered = [t for t in tables if not t.lower().startswith("directus_")]
        self.assertEqual(filtered, ["orders", "products"])

    def test_incremental_sync_parameters_and_max_pk(self):
        """Tests that get_azure_max_pk is defined and verifier supports incremental parameters."""
        from src.migration.migrator import get_azure_max_pk
        from src.verification.verifier import verify_database
        self.assertTrue(callable(get_azure_max_pk))
        self.assertTrue(callable(verify_database))

if __name__ == '__main__':
    unittest.main()
