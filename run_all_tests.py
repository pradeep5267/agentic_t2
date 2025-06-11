#!/usr/bin/env python3
"""
Run all tests for the project.

This script runs both the unittest-based tests for the aio_t14b_mk2.py module
and the pytest-based tests for the Flask application.
"""
import subprocess
import sys
import time
import argparse
import os

def run_tests(args):
    """Run all test files or selected ones based on arguments."""
    # Define all test files
    unit_test_files = [
        "test_aio_t14b_mk2.py", 
        "test_gps_tracking.py",
        "test_database_integration.py",
        "test_integration.py"  # Added the new integration test
    ]
    
    pytest_files = [
        "test_app.py"  # Flask application tests
    ]
    
    # Filter tests if specific modules are requested
    if args.modules:
        modules = args.modules.split(',')
        unit_test_files = [f for f in unit_test_files if any(m in f for m in modules)]
        pytest_files = [f for f in pytest_files if any(m in f for m in modules)]
    
    if not args.app_only:
        # Run unittest tests
        for test_file in unit_test_files:
            if not os.path.exists(test_file):
                print(f"Warning: Test file {test_file} not found, skipping.")
                continue
                
            print(f"\n{'='*80}\nRunning tests in {test_file}\n{'='*80}")
            start_time = time.time()
            
            cmd = [sys.executable, "-m", "unittest"]
            if args.verbose:
                cmd.append("-v")
            cmd.append(test_file)
            
            result = subprocess.run(cmd)
            
            elapsed = time.time() - start_time
            print(f"Completed in {elapsed:.2f} seconds")
            
            if result.returncode != 0:
                print(f"Tests in {test_file} failed!")
                if not args.continue_on_error:
                    return result.returncode
    
    if not args.unit_only:
        # Run pytest tests
        for test_file in pytest_files:
            if not os.path.exists(test_file):
                print(f"Warning: Test file {test_file} not found, skipping.")
                continue
                
            print(f"\n{'='*80}\nRunning pytest tests in {test_file}\n{'='*80}")
            start_time = time.time()
            
            cmd = [sys.executable, "-m", "pytest"]
            if args.verbose:
                cmd.append("-v")
            cmd.append(test_file)
            
            result = subprocess.run(cmd)
            
            elapsed = time.time() - start_time
            print(f"Completed in {elapsed:.2f} seconds")
            
            if result.returncode != 0:
                print(f"Tests in {test_file} failed!")
                if not args.continue_on_error:
                    return result.returncode
    
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run all tests for the project")
    parser.add_argument("-v", "--verbose", action="store_true", help="Run tests in verbose mode")
    parser.add_argument("-c", "--continue-on-error", action="store_true", help="Continue running tests even if some fail")
    parser.add_argument("-m", "--modules", help="Comma-separated list of module names to test (e.g. gps,database,integration)")
    parser.add_argument("--unit-only", action="store_true", help="Run only the unittest tests")
    parser.add_argument("--app-only", action="store_true", help="Run only the pytest Flask app tests")
    
    args = parser.parse_args()
    sys.exit(run_tests(args))