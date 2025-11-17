package org.study.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.client.RestClient;

@Configuration
public class HttpClientConfig {
    
    @Value("${bithumb.api-url}")
    private String apiUrl;
    
    @Bean
    public RestClient bithumbRestClient(RestClient.Builder builder) {
        return builder
            .baseUrl(apiUrl)
            .build();
    }
}