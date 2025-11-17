package org.study.common;

import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestClient;
import org.study.config.AuthToken;

/**
 * 1. ClassName     : HttpService
 * 2. FileName      : HttpService.java
 * 3. Package       : org.study.common
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 17. 오후 8:53
 * 6. Modified Date : 25. 11. 17. 오후 8:53
 * 7. Comment       :
 */
@Service
@RequiredArgsConstructor
public class HttpService {
    
    private final RestClient restClient;
    
    
    public String client(String method, String path, MultiValueMap<String, String> headers) {
        HttpMethod httpMethod = "post".equalsIgnoreCase(method) ? HttpMethod.POST : HttpMethod.GET;
        String[] segments = path.split("/");
        
        ResponseEntity<String> entity = restClient
            .method(httpMethod)
            .uri(uriBuilder -> uriBuilder.path("v1").pathSegment(segments).build())
            .headers(httpHeaders ->  httpHeaders.putAll(headers))
            .retrieve()
            .toEntity(String.class);
        
        // 1) 상태코드 검증
        if (!entity.getStatusCode().is2xxSuccessful()) {
            throw new IllegalStateException("HTTP 에러: " + entity.getStatusCode());
        }
        
        // 2) 바디 검증
        String json = entity.getBody();
        if (json == null || json.isBlank()) {
            throw new IllegalStateException("응답 바디 없음");
        }
        
        return json;
    }
}
