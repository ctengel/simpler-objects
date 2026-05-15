"""
Test script to verify Phase 1 implementation
Tests that Content-Digest, Repr-Digest, and Content-Type headers are correctly returned
"""

import requests
import sys

BASE_URL = "http://localhost:29171"
BUCKET = "test-bucket"
TEST_FILE = "test-object.bin"
TEST_CONTENT = b"Hello, World!"

def test_put_object():
    """Test PUT request returns both digest headers"""
    print(f"[PUT] Uploading {TEST_FILE} to {BUCKET}...")
    url = f"{BASE_URL}/{BUCKET}/{TEST_FILE}"
    
    try:
        response = requests.put(url, data=TEST_CONTENT)
        
        if response.status_code != 201:
            print(f"❌ Expected 201, got {response.status_code}")
            return False
        
        # Check for both digest headers
        repr_digest = response.headers.get('Repr-Digest')
        content_digest = response.headers.get('Content-Digest')
        
        print(f"  Status: {response.status_code} ✓")
        print(f"  Repr-Digest: {repr_digest}")
        print(f"  Content-Digest: {content_digest}")
        
        if not repr_digest:
            print("❌ Missing Repr-Digest header")
            return False
        if not content_digest:
            print("❌ Missing Content-Digest header")
            return False
        if repr_digest != content_digest:
            print("❌ Digest headers don't match")
            return False
        
        print("✅ PUT test passed\n")
        return True
        
    except Exception as e:
        print(f"❌ PUT request failed: {e}\n")
        return False

def test_get_object():
    """Test GET request returns both digest headers and Content-Type"""
    print(f"[GET] Retrieving {TEST_FILE} from {BUCKET}...")
    url = f"{BASE_URL}/{BUCKET}/{TEST_FILE}"
    
    try:
        response = requests.get(url)
        
        if response.status_code != 200:
            print(f"❌ Expected 200, got {response.status_code}")
            return False
        
        # Check for digest headers
        repr_digest = response.headers.get('Repr-Digest')
        content_digest = response.headers.get('Content-Digest')
        content_type = response.headers.get('Content-Type')
        
        print(f"  Status: {response.status_code} ✓")
        print(f"  Repr-Digest: {repr_digest}")
        print(f"  Content-Digest: {content_digest}")
        print(f"  Content-Type: {content_type}")
        print(f"  Content-Length: {response.headers.get('Content-Length')}")
        
        if not repr_digest:
            print("❌ Missing Repr-Digest header")
            return False
        if not content_digest:
            print("❌ Missing Content-Digest header")
            return False
        if not content_type:
            print("❌ Missing Content-Type header")
            return False
        if repr_digest != content_digest:
            print("❌ Digest headers don't match")
            return False
        if response.content != TEST_CONTENT:
            print(f"❌ Content mismatch. Expected {TEST_CONTENT}, got {response.content}")
            return False
        
        print("✅ GET test passed\n")
        return True
        
    except Exception as e:
        print(f"❌ GET request failed: {e}\n")
        return False

def test_head_object():
    """Test HEAD request returns headers without body"""
    print(f"[HEAD] Checking {TEST_FILE}...")
    url = f"{BASE_URL}/{BUCKET}/{TEST_FILE}"
    
    try:
        response = requests.head(url)
        
        if response.status_code != 200:
            print(f"❌ Expected 200, got {response.status_code}")
            return False
        
        repr_digest = response.headers.get('Repr-Digest')
        content_digest = response.headers.get('Content-Digest')
        content_type = response.headers.get('Content-Type')
        
        print(f"  Status: {response.status_code} ✓")
        print(f"  Repr-Digest: {repr_digest}")
        print(f"  Content-Digest: {content_digest}")
        print(f"  Content-Type: {content_type}")
        
        if not repr_digest or not content_digest or not content_type:
            print("❌ Missing headers")
            return False
        if response.content:
            print("❌ HEAD response should not have body")
            return False
        
        print("✅ HEAD test passed\n")
        return True
        
    except Exception as e:
        print(f"❌ HEAD request failed: {e}\n")
        return False

def test_mime_types():
    """Test MIME type detection"""
    print("[MIME Type Detection]")
    test_cases = [
        ("file.txt", "text/plain"),
        ("image.png", "image/png"),
        ("archive.tar.gz", "application/gzip"),
        ("unknown.xyz", "application/octet-stream"),
        ("file.bin", "application/octet-stream"),
    ]
    
    all_passed = True
    for filename, expected_mime in test_cases:
        url = f"{BASE_URL}/{BUCKET}/{filename}"
        try:
            # PUT a small file
            requests.put(url, data=b"test")
            # GET to check Content-Type
            response = requests.get(url)
            actual_mime = response.headers.get('Content-Type')
            
            if actual_mime == expected_mime:
                print(f"  ✓ {filename} → {actual_mime}")
            else:
                print(f"  ❌ {filename} → Expected {expected_mime}, got {actual_mime}")
                all_passed = False
        except Exception as e:
            print(f"  ❌ {filename} failed: {e}")
            all_passed = False
    
    print()
    return all_passed

if __name__ == "__main__":
    print("=" * 60)
    print("Phase 1 Implementation Tests")
    print("=" * 60)
    print()
    
    results = []
    results.append(("PUT object", test_put_object()))
    results.append(("GET object", test_get_object()))
    results.append(("HEAD object", test_head_object()))
    results.append(("MIME types", test_mime_types()))
    
    print("=" * 60)
    print("Test Results:")
    print("=" * 60)
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{test_name}: {status}")
    
    print()
    print(f"Total: {passed}/{total} passed")
    
    if passed == total:
        print("\n🎉 All Phase 1 tests passed!")
        sys.exit(0)
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
        sys.exit(1)
