package org.study.config;

import com.auth0.jwt.JWT;
import com.auth0.jwt.algorithms.Algorithm;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

import java.util.UUID;

/**
 * 1. ClassName     : AuthToken
 * 2. FileName      : AuthToken.java
 * 3. Package       : org.study.config
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 17. 오후 5:42
 * 6. Modified Date : 25. 11. 17. 오후 5:42
 * 7. Comment       :
 */
@Service
@RequiredArgsConstructor
public class AuthToken {
    private final BithumbProperties bithumbProperties;
    
    public String getAuthToken() {
        String accessKey = bithumbProperties.getAccessKey();
        String secretKey = bithumbProperties.getSecretKey();
        
        // Generate access token
        Algorithm algorithm = Algorithm.HMAC256(secretKey);
        String jwtToken = JWT.create()
            .withClaim("access_key", accessKey)
            .withClaim("nonce", UUID.randomUUID().toString())
            .withClaim("timestamp", System.currentTimeMillis())
            .sign(algorithm);
        return "Bearer " + jwtToken;
    }
}
