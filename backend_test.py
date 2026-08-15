#!/usr/bin/env python3
"""
Backend API Tests for Fix 0.5 and ai_rewards-Fix
Tests the following endpoints:
- GET /api/ai/rewards
- POST /api/ai/rewards/backfill (with and without auth)
"""

import requests
import sys
import os

# Get backend URL from environment
BACKEND_URL = "https://c950bfd4-4e9a-4406-8b91-96921c19a170.preview.emergentagent.com"
ADMIN_USER = "Admin"
ADMIN_PASSWORD = "Dean06Greif!/Admin"

def test_ai_rewards_get():
    """Test GET /api/ai/rewards - should return 200 with required keys"""
    print("\n=== Test 1: GET /api/ai/rewards ===")
    try:
        response = requests.get(f"{BACKEND_URL}/api/ai/rewards?days=30", timeout=10)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code != 200:
            print(f"❌ FAIL: Expected 200, got {response.status_code}")
            print(f"Response: {response.text}")
            return False
        
        data = response.json()
        print(f"Response keys: {list(data.keys())}")
        
        required_keys = ["history", "by_regime", "summary"]
        missing_keys = [key for key in required_keys if key not in data]
        
        if missing_keys:
            print(f"❌ FAIL: Missing required keys: {missing_keys}")
            return False
        
        print(f"✅ PASS: GET /api/ai/rewards returned 200 with all required keys")
        print(f"  - history: {len(data.get('history', []))} items")
        by_regime = data.get('by_regime', [])
        if isinstance(by_regime, list):
            print(f"  - by_regime: {len(by_regime)} items")
        else:
            print(f"  - by_regime: {list(by_regime.keys())}")
        print(f"  - summary: {data.get('summary', {})}")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: Exception occurred: {e}")
        return False


def test_backfill_without_auth():
    """Test POST /api/ai/rewards/backfill without auth - should return 401/403"""
    print("\n=== Test 2: POST /api/ai/rewards/backfill (no auth) ===")
    try:
        response = requests.post(f"{BACKEND_URL}/api/ai/rewards/backfill", timeout=10)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code not in [401, 403]:
            print(f"❌ FAIL: Expected 401 or 403, got {response.status_code}")
            print(f"Response: {response.text}")
            return False
        
        print(f"✅ PASS: POST /api/ai/rewards/backfill without auth returned {response.status_code}")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: Exception occurred: {e}")
        return False


def get_admin_token():
    """Login and get admin token"""
    print("\n=== Getting Admin Token ===")
    try:
        response = requests.post(
            f"{BACKEND_URL}/api/auth/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
            timeout=10
        )
        
        if response.status_code != 200:
            print(f"❌ Login failed: {response.status_code}")
            print(f"Response: {response.text}")
            return None
        
        data = response.json()
        token = data.get("token")
        print(f"✅ Login successful, token obtained")
        return token
        
    except Exception as e:
        print(f"❌ Login exception: {e}")
        return None


def test_backfill_with_auth():
    """Test POST /api/ai/rewards/backfill with admin auth - should return 200"""
    print("\n=== Test 3: POST /api/ai/rewards/backfill (with admin auth) ===")
    
    token = get_admin_token()
    if not token:
        print("❌ FAIL: Could not obtain admin token")
        return False
    
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.post(
            f"{BACKEND_URL}/api/ai/rewards/backfill",
            headers=headers,
            timeout=10
        )
        print(f"Status Code: {response.status_code}")
        
        if response.status_code != 200:
            print(f"❌ FAIL: Expected 200, got {response.status_code}")
            print(f"Response: {response.text}")
            return False
        
        data = response.json()
        print(f"Response: {data}")
        
        if "status" not in data or data["status"] != "success":
            print(f"❌ FAIL: Expected status='success', got {data.get('status')}")
            return False
        
        if "rewarded" not in data:
            print(f"❌ FAIL: Missing 'rewarded' key in response")
            return False
        
        print(f"✅ PASS: POST /api/ai/rewards/backfill with auth returned 200")
        print(f"  - status: {data['status']}")
        print(f"  - rewarded: {data['rewarded']}")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: Exception occurred: {e}")
        return False


def test_ai_status():
    """Test GET /api/ai/status - regression check"""
    print("\n=== Test 4: GET /api/ai/status (regression) ===")
    try:
        response = requests.get(f"{BACKEND_URL}/api/ai/status", timeout=10)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code != 200:
            print(f"❌ FAIL: Expected 200, got {response.status_code}")
            print(f"Response: {response.text}")
            return False
        
        data = response.json()
        print(f"✅ PASS: GET /api/ai/status returned 200")
        print(f"  - enabled: {data.get('enabled')}")
        print(f"  - provider: {data.get('provider')}")
        print(f"  - model: {data.get('model')}")
        return True
        
    except Exception as e:
        print(f"❌ FAIL: Exception occurred: {e}")
        return False


def main():
    print("=" * 70)
    print("Backend API Tests - Fix 0.5 & ai_rewards-Fix")
    print("=" * 70)
    
    results = []
    
    # Run all tests
    results.append(("GET /api/ai/rewards", test_ai_rewards_get()))
    results.append(("POST /api/ai/rewards/backfill (no auth)", test_backfill_without_auth()))
    results.append(("POST /api/ai/rewards/backfill (with auth)", test_backfill_with_auth()))
    results.append(("GET /api/ai/status (regression)", test_ai_status()))
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
