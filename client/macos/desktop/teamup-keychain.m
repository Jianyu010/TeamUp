#import <Foundation/Foundation.h>
#import <Security/Security.h>

#include <stdio.h>
#include <string.h>

enum { RecordLimit = 8192 };
static NSString *const Service = @"org.teamup.desktop.login";
static NSString *const ItemLabel = @"TeamUp sign-in";

static SecKeychainRef userKeychain(void) {
    // TeamUp's ad hoc builds use the user's file-based Keychain.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    if (SecKeychainSetUserInteractionAllowed(false) != errSecSuccess) {
        return NULL;
    }
    SecKeychainRef keychain = NULL;
    OSStatus status = SecKeychainCopyDomainDefault(kSecPreferencesDomainUser, &keychain);
#pragma clang diagnostic pop
    return status == errSecSuccess ? keychain : NULL;
}

static NSDictionary *matchingItem(SecKeychainRef keychain, NSString *account) {
    return @{
        (__bridge id)kSecClass: (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService: Service,
        (__bridge id)kSecAttrAccount: account,
        (__bridge id)kSecMatchSearchList: @[(__bridge id)keychain],
    };
}

static OSStatus readItem(NSDictionary *query, CFTypeRef *value) {
    NSMutableDictionary *request = [query mutableCopy];
    request[(__bridge id)kSecReturnData] = @YES;
    return SecItemCopyMatching((__bridge CFDictionaryRef)request, value);
}

static int getItem(NSDictionary *query) {
    CFTypeRef value = NULL;
    OSStatus status = readItem(query, &value);
    if (status == errSecItemNotFound) return 2;
    if (status != errSecSuccess) return 1;
    NSData *data = CFBridgingRelease(value);
    if (![data isKindOfClass:NSData.class] || !data.length || data.length > RecordLimit) {
        return 1;
    }
    return fwrite(data.bytes, 1, data.length, stdout) == data.length && fflush(stdout) == 0 ? 0 : 1;
}

static BOOL keychainIsUnlocked(SecKeychainRef keychain) {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    SecKeychainStatus status = 0;
    return SecKeychainGetStatus(keychain, &status) == errSecSuccess &&
           (status & kSecUnlockStateStatus) != 0;
#pragma clang diagnostic pop
}

static int putItem(SecKeychainRef keychain, NSDictionary *query) {
    unsigned char input[RecordLimit + 1];
    size_t length = fread(input, 1, sizeof(input), stdin);
    if (ferror(stdin) || !length || length > RecordLimit) return 1;
    NSDictionary *changes = @{(__bridge id)kSecValueData: [NSData dataWithBytes:input length:length]};

    CFTypeRef previous = NULL;
    OSStatus status = readItem(query, &previous);
    if (status == errSecSuccess) {
        CFRelease(previous);
        status = SecItemUpdate((__bridge CFDictionaryRef)query,
                               (__bridge CFDictionaryRef)changes);
    } else if ((status == errSecAuthFailed || status == errSecInteractionNotAllowed) &&
               keychainIsUnlocked(keychain)) {
        // An ad hoc rebuild can lose read access to an item created by an older binary.
        // An explicit save replaces that item, so Keychain trusts the current build.
        status = SecItemDelete((__bridge CFDictionaryRef)query);
        if (status == errSecSuccess) status = errSecItemNotFound;
    }
    if (status == errSecItemNotFound) {
        NSMutableDictionary *attributes = [query mutableCopy];
        [attributes removeObjectForKey:(__bridge id)kSecMatchSearchList];
        attributes[(__bridge id)kSecUseKeychain] = (__bridge id)keychain;
        attributes[(__bridge id)kSecAttrLabel] = ItemLabel;
        [attributes addEntriesFromDictionary:changes];
        status = SecItemAdd((__bridge CFDictionaryRef)attributes, NULL);
        if (status == errSecDuplicateItem) {
            status = SecItemUpdate((__bridge CFDictionaryRef)query,
                                   (__bridge CFDictionaryRef)changes);
        }
    }
    return status == errSecSuccess ? 0 : 1;
}

static BOOL validAccount(const char *value) {
    return strlen(value) == 64 && strspn(value, "0123456789abcdef") == 64;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3 || !validAccount(argv[2])) return 1;
        NSString *operation = [NSString stringWithUTF8String:argv[1]];
        SecKeychainRef keychain = userKeychain();
        if (!keychain) return 1;
        NSDictionary *query = matchingItem(keychain, [NSString stringWithUTF8String:argv[2]]);
        int result = 1;
        if ([operation isEqualToString:@"get"]) {
            result = getItem(query);
        } else if ([operation isEqualToString:@"put"]) {
            result = putItem(keychain, query);
        } else if ([operation isEqualToString:@"forget"]) {
            OSStatus status = SecItemDelete((__bridge CFDictionaryRef)query);
            result = status == errSecSuccess || status == errSecItemNotFound ? 0 : 1;
        }
        CFRelease(keychain);
        return result;
    }
}
