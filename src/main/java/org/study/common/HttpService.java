package org.study.common;

import jakarta.annotation.Nullable;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestClient;

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
    
    
    public String client(String method, String path, @Nullable MultiValueMap<String, String> headers, @Nullable MultiValueMap<String, String> params) {
        HttpMethod httpMethod = "post".equalsIgnoreCase(method) ? HttpMethod.POST : HttpMethod.GET;
        String[] segments = path.split("/");
        
        ResponseEntity<String> entity = restClient.method(httpMethod).uri(uriBuilder -> {
            var builder = uriBuilder.path("v1").pathSegment(segments);
            
            // ✅ query param 처리
            if (params != null && !params.isEmpty()) {
                params.forEach((name, values) -> builder.queryParam(name, values.toArray()));
            }
            
            return builder.build();
        }).headers((httpHeaders -> {
            // ✅ header null-safe 처리
            if (headers != null && !headers.isEmpty()) {
                httpHeaders.putAll(headers);
            }
        })).retrieve().toEntity(String.class);
        
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
